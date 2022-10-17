"""Execute tests scripts and interact with test data"""

import filecmp
import sys
import flags
import unittest
from vast_invoke import Exit, task, Context
import time
import json
import os
import io
import common
import requests


# A common prefix for all test files to help cleanup
TEST_PREFIX = "vastcloudtest"

# A script whose output should be "hello world"
DUMMY_SCRIPT = """
VAR1="hello"
echo -n $VAR1
echo -n " "
echo -n $VAR2
"""

# Force stack trace display for all tests
os.environ[flags.TRACE_FLAG_VAR] = "1"


def vast_import_suricata(c: Context):
    """Import Suricata data from the provided URL"""
    url = "https://raw.githubusercontent.com/tenzir/vast/v1.0.0/vast/integration/data/suricata/eve.json"
    c.run(
        f"./vast-cloud vast.server-execute -c 'wget -O - -o /dev/null {url} | vast import suricata'"
    )


def clean(c):
    """Delete local and remote files starting with the TEST_PREFIX"""
    print("Cleaning up...")
    tmp_prefix = f"/tmp/{TEST_PREFIX}*"
    c.run(f"rm -rfv {common.container_path(tmp_prefix)}")
    bucket_name = c.run("./vast-cloud workbucket.name", hide="out").stdout.rstrip()
    c.run(
        f'aws s3 rm --recursive s3://{bucket_name}/ --exclude "*" --include "{TEST_PREFIX}*" --region {common.AWS_REGION()}'
    )


def start_vast(c):
    """Start the server, noop if already running"""
    print("Start VAST Server")
    c.run("./vast-cloud vast.start-server")
    # wait a sec to be sure the vast server is running
    time.sleep(1)


class VastCloudTestLoader(unittest.TestLoader):
    """Load TestCase instances with an additional Context attribute"""

    def __init__(self, invoke_ctx: Context) -> None:
        self.c = invoke_ctx
        super().__init__()

    def loadTestsFromTestCase(self, testCaseClass):
        """Override load method to add context"""
        testCaseNames = self.getTestCaseNames(testCaseClass)
        testCases = []
        for testCaseName in testCaseNames:
            testCase = testCaseClass(testCaseName)
            testCase.c = self.c
            testCases.append(testCase)
        return self.suiteClass(testCases)


class VastStartRestart(unittest.TestCase):
    def test(self):
        """Validate VAST server start and restart commands"""
        print("Run vast.start-server")
        start_out = self.c.run("./vast-cloud vast.start-server", hide="out").stdout
        # We don't assert intermediate statuses, the server might be already running
        self.assertIn("-> RUNNING", start_out)

        print("VAST server status")
        status_out = self.c.run("./vast-cloud vast.server-status", hide="out").stdout
        self.assertEqual("Service running", status_out.strip())

        print("Run vast.start-server again")
        start_out = self.c.run("./vast-cloud vast.start-server", hide="out").stdout
        self.assertNotIn("-> PROVISIONING", start_out)
        self.assertNotIn("-> PENDING", start_out)
        self.assertNotIn("-> ACTIVATING", start_out)
        self.assertIn("-> RUNNING", start_out)

        print("Run vast.restart-server")
        restart_out = self.c.run("./vast-cloud vast.restart-server", hide="out").stdout
        self.assertIn("-> DEACTIVATING", restart_out)
        self.assertIn("-> STOPPING", restart_out)
        self.assertIn("-> DEPROVISIONING", restart_out)
        self.assertIn("-> PROVISIONING", restart_out)
        self.assertIn("-> PENDING", restart_out)
        self.assertIn("-> ACTIVATING", restart_out)
        self.assertIn("-> RUNNING", restart_out)

        print("VAST server status")
        status_out = self.c.run("./vast-cloud vast.server-status", hide="out").stdout
        self.assertEqual("Service running", status_out.strip())


class LambdaOutput(unittest.TestCase):
    def test(self):
        """Validate VAST server start and restart commands"""
        print("Running successful command")
        success = self.c.run(
            "./vast-cloud vast.lambda-client -c 'echo hello' -e VAR1=val1", hide="out"
        ).stdout
        self.assertEqual(
            success,
            """PARSED COMMAND:
['/bin/bash', '-c', 'echo hello']

ENV:
{'VAR1': 'val1'}

STDOUT:
hello

STDERR:


RETURN CODE:
0
""",
        )

        print("Running erroring command")
        error = self.c.run(
            "./vast-cloud vast.lambda-client -c 'echo hello >&2 && false' -e VAR1=val1",
            hide="out",
            warn=True,
        ).stdout
        self.assertEqual(
            error,
            """PARSED COMMAND:
['/bin/bash', '-c', 'echo hello >&2 && false']

ENV:
{'VAR1': 'val1'}

STDOUT:


STDERR:
hello

RETURN CODE:
1
""",
        )


class VastDataImport(unittest.TestCase):
    def setUp(self):
        start_vast(self.c)

    def vast_count(self):
        """Run `vast count` and parse the result"""
        res_raw = self.c.run(
            './vast-cloud vast.lambda-client -c "vast count" --json-output',
            hide="stdout",
        )
        res_obj = json.loads(res_raw.stdout)
        self.assertEqual(
            res_obj["parsed_cmd"],
            [
                "/bin/bash",
                "-c",
                "vast count",
            ],
            "Unexpected parsed command",
        )
        self.assertTrue(res_obj["stdout"].isdigit(), "Count result is not a number")
        return int(res_obj["stdout"])

    def test(self):
        """Import data from a file and check count increase"""
        print("Import data into VAST")
        init_count = self.vast_count()
        vast_import_suricata(self.c)
        new_count = self.vast_count()
        print(f"Expected count increase 7, got {new_count-init_count}")
        self.assertEqual(7, new_count - init_count, "Wrong count")


class WorkbucketRoundtrip(unittest.TestCase):
    def validate_dl(self, src, dst, key):
        print(f"Download object {key} to {dst}")
        self.c.run(
            f"./vast-cloud workbucket.download --destination={dst} --key={key}",
            hide="out",
        )
        self.assertTrue(
            filecmp.cmp(common.container_path(src), common.container_path(dst)),
            f"{src} and {dst} are not identical",
        )

    def setUp(self):
        clean(self.c)

    def tearDown(self):
        clean(self.c)

    def test(self):
        """Validate that the roundtrip upload/download works"""
        key_fromfile = f"{TEST_PREFIX}_fromfile"
        key_frompipe = f"{TEST_PREFIX}_frompipe"
        src = f"/tmp/{TEST_PREFIX}_src"
        dst_fromfile = f"/tmp/{TEST_PREFIX}_fromfile_dst"
        dst_frompipe = f"/tmp/{TEST_PREFIX}_frompipe_dst"
        self.c.run(f"echo 'hello world' > {common.container_path(src)}")

        print("List before upload")
        empty_ls = self.c.run(
            f"./vast-cloud workbucket.list --prefix={TEST_PREFIX}", hide="out"
        ).stdout
        self.assertEqual(empty_ls, "")

        print(f"Upload from {src} to object {key_fromfile}")
        self.c.run(
            f"./vast-cloud workbucket.upload --source={src} --key={key_fromfile}"
        )

        print(f"Upload from stdin to object {key_frompipe}")
        self.c.run(
            f"echo 'hello world' | ./vast-cloud workbucket.upload --source=/dev/stdin --key={key_frompipe}"
        )

        print("List after upload")
        ls = self.c.run(
            f"./vast-cloud workbucket.list --prefix {TEST_PREFIX}", hide="out"
        ).stdout
        self.assertIn(key_fromfile, ls)
        self.assertIn(key_frompipe, ls)

        self.validate_dl(src, dst_fromfile, key_fromfile)

        self.validate_dl(src, dst_frompipe, key_frompipe)

        print(f"Delete object {key_frompipe}")
        self.c.run(f"./vast-cloud workbucket.delete --key={key_frompipe}", hide="out")
        half_ls = self.c.run(
            f"./vast-cloud workbucket.list --prefix {TEST_PREFIX}", hide="out"
        ).stdout
        self.assertIn(key_fromfile, half_ls)
        self.assertNotIn(key_frompipe, half_ls)


class ScriptedLambda(unittest.TestCase):
    def setUp(self):
        clean(self.c)

    def tearDown(self):
        clean(self.c)

    def test(self):
        """Validate that we can run commands from files"""
        script_file = f"/tmp/{TEST_PREFIX}_script"
        with open(common.container_path(script_file), "w") as text_file:
            text_file.write(DUMMY_SCRIPT)

        print("Run lambda with local script")
        res = self.c.run(
            f"./vast-cloud vast.lambda-client -c file://{script_file} -e VAR2=world --json-output",
            hide="out",
        ).stdout
        self.assertEqual(json.loads(res)["stdout"], "hello world")

        print("Run lambda with piped script")
        res = self.c.run(
            f"cat {common.container_path(script_file)} | ./vast-cloud vast.lambda-client -c file:///dev/stdin -e VAR2=world --json-output",
            hide="out",
        ).stdout
        self.assertEqual(json.loads(res)["stdout"], "hello world")


class ScriptedExecuteCommand(unittest.TestCase):
    def setUp(self):
        clean(self.c)
        start_vast(self.c)

    def tearDown(self):
        clean(self.c)

    def test(self):
        """Validate that we can run commands from files"""
        script_file = f"/tmp/{TEST_PREFIX}_script"
        script_key = f"{TEST_PREFIX}_script"
        with open(common.container_path(script_file), "w") as text_file:
            text_file.write(DUMMY_SCRIPT)

        print("Run execute command with piped script")
        # we can pipe into execute command here because we are in a no pty context
        res = self.c.run(
            f"cat {common.container_path(script_file)} | ./vast-cloud vast.server-execute -c file:///dev/stdin -e VAR2=world",
            hide="out",
        ).stdout
        self.assertIn("hello world", res)

        print("Run execute command with S3 script")
        self.c.run(
            f"./vast-cloud workbucket.upload --source={script_file} --key={script_key}"
        )
        bucket_name = self.c.run(
            "./vast-cloud workbucket.name", hide="out"
        ).stdout.rstrip()
        res = self.c.run(
            f"./vast-cloud vast.server-execute -c s3://{bucket_name}/{script_key} -e VAR2=world",
            hide="out",
        ).stdout
        self.assertIn("hello world", res)


class Common(unittest.TestCase):
    def test_list_modules(self):
        res = common.list_modules(self.c)
        self.assertIn("core-1", res)
        self.assertIn("core-2", res)

    def test_tf_version(self):
        res = common.tf_version(self.c)
        self.assertRegex(res, r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")

    def test_parse_env(self):
        res = common.parse_env(["var1=val1", "var2=val2"])
        self.assertEqual(res, {"var1": "val1", "var2": "val2"})


class MISP(unittest.TestCase):
    def setUp(self):
        print("Start MISP Server")
        self.c.run("./vast-cloud misp.start")

    def wait_for_misp(self, start_time) -> str:
        """Return the body if 200, retry if 502

        This is similar to the retry that would have been performed by the browser"""
        if time.time() - start_time > 300:
            raise Exit("Timeout: Could not reach MISP")
        try:
            resp = requests.get("http://localhost:8080")
            resp.raise_for_status()
            print("Got response from MISP!")
            return resp.text
        except requests.exceptions.HTTPError as e:
            if resp.status_code != 502:
                raise e
            self.assertIn("Waiting for MISP to start...", resp.text)
            time.sleep(5)
            return self.wait_for_misp(start_time)
        except requests.exceptions.TooManyRedirects:
            print("Got Too Many Redirects error, got to retry...")
            # TODO find a solution to avoid this transient error state
            time.sleep(5)
            return self.wait_for_misp(start_time)

    def test(self):
        """Test that we can get the MISP login page"""
        status_out = self.c.run("./vast-cloud misp.status", hide="out").stdout
        self.assertEqual("Service running", status_out.strip())

        self.c.run("nohup ./vast-cloud misp.tunnel > /dev/null 2>&1 &")
        time.sleep(30)
        try:
            body = self.wait_for_misp(time.time())
            self.assertIn("<title>Users - MISP</title>", body)
        finally:
            self.c.run(
                "docker stop $(docker ps --no-trunc | grep misp.tunnel | cut -d ' ' -f 1)"
            )

    def tearDown(self):
        print("Stop MISP Server")
        self.c.run("./vast-cloud misp.stop")


@task(
    help={"case": "The case class names to execute, all tests if unspecified"},
    iterable=["case"],
)
def run(c, case=[]):
    """Run the tests, either inidividually or in bulk

    Notes:
    - VAST needs to be deployed beforehand with the workbucket plugin
    - These tests will affect the state of the current stack"""
    mod = sys.modules[__name__]
    if len(case) == 0:
        suite = VastCloudTestLoader(c).loadTestsFromModule(mod)
    else:
        suite = VastCloudTestLoader(c).loadTestsFromNames(case, mod)
    res = unittest.TextTestRunner().run(suite)
    if not res.wasSuccessful():
        exit(1)


DATASETS = {
    "suricata": {
        "path": f"{common.REPOROOT}/vast/integration/data/suricata/eve.json",
        "pipe": "cat",  # use cat for noop
        "import_cmd": "vast import suricata",
    },
    "flowlogs": {
        "path": f"{common.RESOURCEDIR}/testdata/flowlogs.csv",
        "pipe": "python3 -c 'import csv, json, sys; [print(json.dumps(dict(r))) for r in csv.DictReader(sys.stdin, delimiter=\" \")]'",
        "import_cmd": "vast import --type=aws.flowlogs json",
    },
    "cloudtrail": {
        "path": f"{common.RESOURCEDIR}/testdata/cloudtrail.json",
        "pipe": "jq  -c '.Records[]'",
        "import_cmd": "vast import --type=aws.cloudtrail json",
    },
}


@task(help={"dataset": f"One of {list(DATASETS.keys())}"})
def import_data(c, dataset):
    """Import the given dataset into the running vast server. Requires a workbucket."""
    if dataset not in DATASETS:
        raise Exit(message=f"--dataset should be one of {list(DATASETS.keys())}")
    ds_opts = DATASETS[dataset]
    ds_key = f"testdataset/{dataset}"
    with open(ds_opts["path"], "rb") as f:
        c.run(f"./vast-cloud workbucket.upload -s /dev/stdin -k {ds_key}", in_stream=f)
    print(f"Test data uploaded to workbucket as {ds_key}")
    bucket_name = c.run("./vast-cloud workbucket.name", hide="out").stdout.rstrip()
    cmd = f"""aws s3 cp s3://{bucket_name}/{ds_key} - | {ds_opts["pipe"]} | {ds_opts["import_cmd"]}"""
    c.run(
        "./vast-cloud vast.lambda-client -c file:///dev/stdin",
        in_stream=io.StringIO(cmd),
    )
