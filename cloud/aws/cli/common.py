from functools import cache
import sys
from typing import Dict, List, Set
from flags import TRACE
from vast_invoke import Context, Exit, Failure
import dynaconf
import json
import re
import os
import time
import boto3.session
import boto3
import botocore.client
import shutil


@cache
def s3_regions():
    """List all the regions where S3 is available using the AWS API"""
    return boto3.session.Session().get_available_regions("s3")


AWS_REGION_VALIDATOR = dynaconf.Validator(
    "VAST_AWS_REGION", must_exist=True, is_in=s3_regions()
)

EXIT_CODE_TASK_NOT_RUNNING = 8

# Path aliases
CLOUDROOT = "."
REPOROOT = "../.."
TFDIR = f"{CLOUDROOT}/terraform"
DOCKERDIR = f"{CLOUDROOT}/docker"
RESOURCEDIR = f"{CLOUDROOT}/resources"
HOSTROOT = "/host"


def conf(validators=[]) -> dict:
    """Load variables from both the environment and the .env file if:
    - their key is prefixed with either VAST_, TF_ or AWS_"""
    dc = dynaconf.Dynaconf(
        load_dotenv=True,
        envvar_prefix=False,
        validators=validators,
    )
    return {
        k: v
        for (k, v) in dc.as_dict().items()
        if k.startswith(("VAST_", "TF_", "AWS_"))
    }


def auto_app_fmt(val: bool) -> str:
    """Format the CLI options for auto approve"""
    if val:
        return "--terragrunt-non-interactive --auto-approve"
    else:
        return ""


def list_modules(c: Context) -> List[str]:
    """List available Terragrunt modules"""
    return [
        mod
        for mod in os.listdir(TFDIR)
        if os.path.isfile(f"{TFDIR}/{mod}/terragrunt.hcl")
    ]


def active_plugins() -> Set[str]:
    """Cloud CLI plugins activated"""
    plugin_var = conf([dynaconf.Validator("VAST_CLOUD_PLUGINS", default="")])[
        "VAST_CLOUD_PLUGINS"
    ]
    plugin_set = {plugin.strip() for plugin in plugin_var.split(",")}
    plugin_set.discard("")
    return plugin_set


def active_modules(c: Context) -> Set[str]:
    """Terragrunt modules activated and core modules"""
    return {*active_plugins().intersection(list_modules(c)), "core-1", "core-2"}


def tf_version(c: Context):
    """Terraform version used by the CLI"""
    version_json = c.run("terraform version -json", hide="out").stdout
    return json.loads(version_json)["terraform_version"]


def terraform_output(c: Context, step, key) -> str:
    cmd = f"terraform -chdir={TFDIR}/{step} output --raw {key}"
    try:
        output = c.run(
            cmd,
            hide=True,
            # avoid unintentionally capturing stdin
            in_stream=False,
        ).stdout
        # `terraform output` sometimes raises errors, sometimes only prints
        # warnings, according to the actual output state. Here, we streamline
        # both cases into a single exit message.
        if "No outputs found" in output:
            raise Exit(output)
    except Failure as e:
        _, err = e.streams_for_display()
        if TRACE:
            print(cmd, file=sys.stderr)
            print(err.strip(), file=sys.stderr)
        raise Exit(
            f"The step '{step}' was not deployed, is not up to date, "
            + f"or is improperly initialized (Terraform output '{key}' not found)",
            code=1,
        )
    return output


def AWS_REGION():
    return conf(AWS_REGION_VALIDATOR)["VAST_AWS_REGION"]


def aws(service, resource=False):
    # timeout set to 1000 to be larger than lambda max duration
    config = botocore.client.Config(retries={"max_attempts": 0}, read_timeout=1000)
    if resource:
        return boto3.resource(service, region_name=AWS_REGION(), config=config)
    else:
        return boto3.client(service, region_name=AWS_REGION(), config=config)


def container_path(host_path: str):
    """Convert the given path on the host its location once mounted in the CLI container"""
    return f"{HOSTROOT}{host_path}"


def check_absolute(path: str):
    """Raise an Exit if the provided path is not absolute"""
    if not os.path.isabs(path):
        raise Exit(f"{path} is not an absolute path")


def load_cmd(cmd: str) -> bytes:
    """Load the command as bytes. If cmd starts with file:// or s3://, load commands from a file.

    Must be an absolute path (e.g file:///etc/mycommands)"""
    if cmd.startswith("file://"):
        path = cmd[7:]
        check_absolute(path)
        with open(container_path(path), "rb") as f:
            return f.read()
    if cmd.startswith("s3://"):
        chunks = cmd[5:].split("/", 1)
        return aws("s3").get_object(Bucket=chunks[0], Key=chunks[1])["Body"].read()
    else:
        return cmd.encode()


def parse_env(env: List[str]) -> Dict[str, str]:
    """Convert a list of "key=value" strings to a dictionary of {key: value}"""

    def split_name_val(name_val):
        env_split = name_val.split("=")
        if len(env_split) != 2:
            raise Exit(f"{name_val} should have exactly one '=' char", 1)
        return env_split[0], env_split[1]

    name_val_list = [split_name_val(v) for v in env]
    return {v[0]: v[1] for v in name_val_list}


def clean_modules():
    """Delete Terragrunt and Terragrunt cache files. This does not impact the Terraform state"""
    for path in os.listdir(TFDIR):
        if os.path.isdir(f"{TFDIR}/{path}"):
            # clean terraform cache
            tf_cache = f"{TFDIR}/{path}/.terraform"
            if os.path.isdir(tf_cache):
                print(f"deleting {tf_cache}")
                shutil.rmtree(tf_cache)
            # remove generated files
            for sub_path in os.listdir(f"{TFDIR}/{path}"):
                if sub_path.endswith(".generated.tf"):
                    generated_file = f"{TFDIR}/{path}/{sub_path}"
                    print(f"deleting {generated_file}")
                    os.remove(generated_file)


## Task management


class FargateService:
    """A service with a single task running"""

    def __init__(self, fargate_cluster_name, service_name, task_family):
        self.cluster = fargate_cluster_name
        self.service_name = service_name
        self.task_family = task_family

    def get_task_id(self, max_wait_time_sec=0, start_time=None):
        """Get the task id for this service. If no server is running, it waits
        until max_wait_time_sec for a new server to be started."""
        if start_time == None:
            start_time = time.time()
        while True:
            task_res = aws("ecs").list_tasks(
                family=self.task_family, cluster=self.cluster
            )
            nb_vast_tasks = len(task_res["taskArns"])
            if nb_vast_tasks == 1:
                task_id = task_res["taskArns"][0].split("/")[-1]
                return task_id
            if nb_vast_tasks > 1:
                raise Exit(f"{nb_vast_tasks} tasks running", 1)
            if max_wait_time_sec == 0:
                raise Exit(f"No task running", EXIT_CODE_TASK_NOT_RUNNING)
            if time.time() - start_time > max_wait_time_sec:
                raise Exit(f"Task timed out", 1)
            time.sleep(1)

    def _task_desc(self, task_arn):
        return aws("ecs").describe_tasks(cluster=self.cluster, tasks=[task_arn])[
            "tasks"
        ][0]

    def service_status(self):
        """Get the status of the service and the associated task"""
        # describe task
        task_res = aws("ecs").list_tasks(family=self.task_family, cluster=self.cluster)
        nb_vast_tasks = len(task_res["taskArns"])
        if nb_vast_tasks == 1:
            task_status = self._task_desc(task_res["taskArns"][0])["lastStatus"]
        # describe service
        srv_res = aws("ecs").describe_services(
            cluster=self.cluster, services=[self.service_name]
        )
        desired_tasks = srv_res["services"][0]["desiredCount"]

        # state machine
        if nb_vast_tasks > 1:
            return f"Unexpected number of tasks: {nb_vast_tasks}"
        if desired_tasks == 1 and nb_vast_tasks == 1 and task_status == "RUNNING":
            return "Service running"
        if desired_tasks == 1:
            if nb_vast_tasks == 0:
                task_status = "no task running"
            else:
                task_status = f"task status: {task_status}"
            return f"Service starting... ({task_status})"
        if desired_tasks == 0 and nb_vast_tasks == 1:
            return f"Service stopping... (task status: {task_status})"
        if desired_tasks == 0:
            return "Service stopped"

    def describe_task(self):
        """Describe the running tasks, erroring out if there is not exactly one task"""
        task_res = aws("ecs").list_tasks(family=self.task_family, cluster=self.cluster)
        nb_vast_tasks = len(task_res["taskArns"])
        if nb_vast_tasks == 0:
            raise Exit("No task running")
        if nb_vast_tasks > 1:
            raise Exit("{nb_vast_tasks} tasks running")
        else:
            desc = self._task_desc(task_res["taskArns"][0])
            return desc

    def _wait_for_status(
        self, task_id, target_status: str | List[str], timeout, start_time
    ):
        print(f"Waiting for task {task_id} to reach status {target_status}:")
        previous_status = ""
        while time.time() - start_time < timeout:
            status = self._task_desc(task_id)["lastStatus"]
            if previous_status != status:
                print(f"-> {status}")
                previous_status = status
            if status in target_status:
                return
            time.sleep(0.5)
        raise Exit("Timed out")

    def start_service(self, timeout=300):
        """Start the service. Noop if it is already running"""
        start_time = time.time()
        print("Starting service...")
        aws("ecs").update_service(
            cluster=self.cluster, service=self.service_name, desiredCount=1
        )
        task_id = self.get_task_id(timeout, start_time)
        self._wait_for_status(task_id, "RUNNING", timeout, start_time)

    def _stop_task(self, timeout, start_time):
        "Stop the current running task in this service."
        task_id = self.get_task_id()
        print(f"Calling stop on task {task_id}...")
        aws("ecs").stop_task(task=task_id, cluster=self.cluster)
        self._wait_for_status(
            task_id, ["DEPROVISIONING", "STOPPED"], timeout, start_time
        )

    def stop_service(self, timeout=200):
        """Stop the service and its task"""
        start_time = time.time()
        print("Stopping service...")
        aws("ecs").update_service(
            cluster=self.cluster, service=self.service_name, desiredCount=0
        )
        self._stop_task(timeout, start_time)

    def restart_service(self, timeout=500):
        """Stop the task within the service, the service starts a new one"""
        start_time = time.time()
        self._stop_task(timeout, start_time)
        task_id = self.get_task_id(timeout, start_time)
        self._wait_for_status(task_id, "RUNNING", timeout, start_time)
