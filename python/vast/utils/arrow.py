import json
import ipaddress as ip
from typing import SupportsBytes

import pyarrow as pa


class PatternScalar(pa.ExtensionScalar):
    def as_py(self) -> str:
        return self.value.as_py()


class PatternType(pa.ExtensionType):
    ext_name = "vast.pattern"
    ext_type = pa.string()

    def __init__(self):
        pa.ExtensionType.__init__(self, self.ext_type, self.ext_name)

    def __arrow_ext_serialize__(self) -> bytes:
        return self.ext_name.encode()

    @classmethod
    def __arrow_ext_deserialize__(cls, storage_type, serialized: bytes):
        if serialized.decode() != cls.ext_name:
            raise TypeError("type identifier does not match")
        if storage_type != cls.ext_type:
            raise TypeError("storage type does not match")
        return PatternType()

    def __reduce__(self):
        return PatternScalar, ()

    def __arrow_ext_scalar_class__(self):
        return PatternScalar


class AddressScalar(pa.ExtensionScalar):
    def as_py(self) -> ip.IPv4Address | ip.IPv6Address:
        return unpack_ip(self.value.as_py())


class AddressType(pa.ExtensionType):
    ext_name = "vast.address"
    ext_type = pa.binary(16)

    def __init__(self):
        pa.ExtensionType.__init__(self, self.ext_type, self.ext_name)

    def __arrow_ext_serialize__(self) -> bytes:
        return self.ext_name.encode()

    @classmethod
    def __arrow_ext_deserialize__(cls, storage_type, serialized: bytes):
        if serialized.decode() != cls.ext_name:
            raise TypeError("type identifier does not match")
        if storage_type != cls.ext_type:
            raise TypeError("storage type does not match")
        return AddressType()

    def __reduce__(self):
        return AddressScalar, ()

    def __arrow_ext_scalar_class__(self):
        return AddressScalar


class SubnetScalar(pa.ExtensionScalar):
    def as_py(self) -> ip.IPv4Network | ip.IPv6Network:
        address = self.value[0].as_py()
        length = self.value[1].as_py()
        return ip.ip_network((address, length), strict=False)


class SubnetType(pa.ExtensionType):
    ext_name = "vast.subnet"
    ext_type = pa.struct([("address", AddressType()), ("length", pa.uint8())])

    def __init__(self):
        pa.ExtensionType.__init__(self, self.ext_type, self.ext_name)

    def __arrow_ext_serialize__(self) -> bytes:
        return self.ext_name.encode()

    @classmethod
    def __arrow_ext_deserialize__(cls, storage_type, serialized: bytes):
        if serialized.decode() != cls.ext_name:
            raise TypeError("type identifier does not match")
        if storage_type != cls.ext_type:
            raise TypeError("storage type does not match")
        return SubnetType()

    def __reduce__(self):
        return SubnetScalar, ()

    def __arrow_ext_scalar_class__(self):
        return SubnetScalar


class EnumScalar(pa.ExtensionScalar):
    def as_py(self) -> str:
        return self.value.as_py()


class EnumType(pa.ExtensionType):
    ext_name = "vast.enumeration"
    # VAST's flatbuffer type representation uses a 32-bit unsigned integer. We
    # use an 8-bit type here only for backwards compatibility to the legacy
    # type. Eventually this will be a 32-bit type as well.
    ext_type = pa.dictionary(pa.uint8(), pa.string())

    def __init__(self, fields: dict[str, int]):
        # We're optimizing for use cases that involve reading the integer
        # representation of enums from VAST, so we keep the key-name mappings in
        # the inverse order in memory.
        self._fields = {v: k for k, v in fields.items()}
        pa.ExtensionType.__init__(self, self.ext_type, self.ext_name)

    @property
    def fields(self):
        return self._fields

    def field(self, key):
        return self._fields[key]

    def __arrow_ext_serialize__(self) -> bytes:
        inverse = {v: k for k, v in self._fields.items()}
        return json.dumps(inverse).encode()
        # We're optimizing for reading from VAST, so we keep the key-name
        # mappings in the inverse order.

    @classmethod
    def __arrow_ext_deserialize__(cls, storage_type, serialized: bytes):
        fields = json.loads(serialized.decode())
        if storage_type != cls.ext_type:
            raise TypeError("storage type does not match")
        return EnumType(fields)

    def __reduce__(self):
        return EnumScalar, ()

    def __arrow_ext_scalar_class__(self):
        return EnumScalar


def names(schema: pa.Schema):
    meta = schema.metadata
    return [meta[key].decode() for key in meta if key.startswith(b"VAST:name:")]


def name(schema: pa.Schema):
    xs = names(schema)
    return xs[0] or ""


def pack_ip(address: str | ip.IPv4Address | ip.IPv6Address) -> bytes:
    match address:
        case str():
            return pack_ip(ip.ip_address(address))
        case ip.IPv4Address():
            prefix = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff"
            return prefix + address.packed
        case ip.IPv6Address():
            return address.packed


# Accepts a 128-bit buffer holding an IPv6 address and returns an IPv4 or IPv6
# address.
def unpack_ip(buffer: SupportsBytes) -> ip.IPv4Address | ip.IPv6Address:
    num = int.from_bytes(buffer, byteorder="big")
    # Convert IPv4 mapped addresses back to regular IPv4.
    # https://tools.ietf.org/html/rfc4291#section-2.5.5.2
    if (num >> 32) == 65535:
        num = num - (65535 << 32)
    return ip.ip_address(num)


# Modules are intialized exactly once, so we can perform the registration here.
pa.register_extension_type(PatternType())
pa.register_extension_type(AddressType())
pa.register_extension_type(EnumType({"stub": 0}))

# FIXME: uncomment once we can depend on a version that includes
# https://github.com/apache/arrow/pull/14106. Until then, we cannot work with
# subnets extension types.
# pa.register_extension_type(SubnetType())
