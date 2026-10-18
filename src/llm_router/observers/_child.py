"""Child entry point for observe_isolated: policy+worker JSON on stdin, one observation JSON line out."""
import json
import sys

from . import common, observe_inline


def main():
    try:
        request = json.loads(sys.stdin.read())
        result = observe_inline(request['policy'], request['worker'])
    except Exception as error:  # never echo details that might include paths or output
        result = common.unknown('observer_error:' + type(error).__name__)
    sys.stdout.write(json.dumps(result) + '\n')
    sys.stdout.flush()


if __name__ == '__main__':
    main()
