"""Versioned JSON Schema contracts and bounded, dependency-free decoding.

One layer covers Stackwich worker reports, batch judge verdict files, and live
unit reviews. Callers own a single format repair; this module only accepts or
rejects a payload.
"""

import json
import os
import stat

VERSION = 1
MAX_REPORT_BYTES = 1_000_000
MAX_BATCH_VERDICT_BYTES = 1024
MAX_UNIT_VERDICT_BYTES = 16_384


class InvalidStructuredOutput(ValueError):
    """An output cannot be used. The code is safe to log without its payload."""


# A present payload that failed the schema. A missing file stays a product failure
# so the next attempt can fail over; it is not an in-cycle format repair.
FORMAT_ERRORS = frozenset({'malformed_json', 'schema_mismatch', 'oversized_output', 'duplicate_field'})


def _object(**properties):
    return {
        'type': 'object', 'properties': properties, 'required': list(properties),
        'additionalProperties': False,
    }


def _array(items, minimum=0):
    return {'type': 'array', 'items': items, 'minItems': minimum}


def _schema(name, **properties):
    return {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        '$id': f'urn:llm-router:{name}:v{VERSION}',
        **_object(schema_version={'type': 'integer', 'const': VERSION}, **properties),
    }


_STRING = {'type': 'string'}
_INTEGER = {'type': 'integer'}
_COUNT = {'type': 'integer', 'minimum': 0}

WORKER_REPORT_V1 = _schema(
    'worker-report',
    plan=_object(files=_array(_STRING, 1), steps=_array(_STRING, 1),
                 verification=_array(_object(command=_STRING, expected=_STRING), 1)),
    execute=_object(changed_files=_array(_STRING, 1)),
    verify=_object(commands_run=_array(_object(command=_STRING, exit_code=_INTEGER), 1),
                   scope_check=_STRING, gate=_STRING),
    review=_object(verdict=_STRING, items=_array(_STRING)),
    cycles={'type': 'integer', 'enum': [1, 2]},
)
BATCH_VERDICT_V1 = _schema('batch-verdict', wrong=_COUNT, crash=_COUNT, nonce=_STRING)
UNIT_VERDICT_V1 = _schema('unit-verdict',
                          verdict={'type': 'string', 'enum': ['approve', 'reject']}, notes=_STRING)

_TYPES = {'object': dict, 'array': list, 'integer': int, 'string': str}


def _matches(value, schema):
    """Validate the JSON Schema keywords used by the contracts in this module."""
    kind = schema['type']
    if type(value) is not _TYPES[kind]:
        return False
    if 'const' in schema and value != schema['const']:
        return False
    if 'enum' in schema and value not in schema['enum']:
        return False
    if 'minimum' in schema and value < schema['minimum']:
        return False
    if kind == 'object':
        properties = schema['properties']
        return set(value) == set(properties) and all(
            _matches(value[name], spec) for name, spec in properties.items())
    if kind == 'array':
        return len(value) >= schema['minItems'] and all(_matches(item, schema['items']) for item in value)
    return True


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise InvalidStructuredOutput('duplicate_field')
    return result


def _reject_constant(_value):
    raise InvalidStructuredOutput('malformed_json')


def parse_json(raw, schema, max_bytes):
    if raw is None:
        raise InvalidStructuredOutput('missing_output')
    if not isinstance(raw, (str, bytes)):
        raise InvalidStructuredOutput('malformed_json')
    if len(raw) > max_bytes:
        raise InvalidStructuredOutput('oversized_output')
    try:
        if isinstance(raw, str):
            raw = raw.encode('utf-8')
        if len(raw) > max_bytes:
            raise InvalidStructuredOutput('oversized_output')
        if not raw.strip():
            raise InvalidStructuredOutput('missing_output')
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except InvalidStructuredOutput:
        raise
    except (ValueError, RecursionError) as error:
        raise InvalidStructuredOutput('malformed_json') from error
    if not _matches(result, schema):
        raise InvalidStructuredOutput('schema_mismatch')
    return result


def load_json(path, schema, max_bytes):
    """Read one bounded regular file without following a final-component symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise InvalidStructuredOutput('unsafe_output')
            raw = handle.read(max_bytes + 1)
    except FileNotFoundError as error:
        raise InvalidStructuredOutput('missing_output') from error
    except OSError as error:
        raise InvalidStructuredOutput('unreadable_output') from error
    return parse_json(raw, schema, max_bytes), raw
