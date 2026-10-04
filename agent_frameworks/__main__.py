import argparse
import json
from .pipeline import collect, ensure_wiki, finish, locked, retry_sources


def main():
    parser = argparse.ArgumentParser(description='Independent Agent Framework daily collection')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('setup')
    c = sub.add_parser('collect')
    c.add_argument('--day', help='First manual run: today in Asia/Shanghai; midnight runs default to the preceding day')
    r = sub.add_parser('retry-sources')
    r.add_argument('--run', required=True)
    f = sub.add_parser('finish')
    f.add_argument('--run', required=True)
    f.add_argument('--decisions')
    f.add_argument('--no-email', action='store_true', help='Testing only; scheduled runs must always send email')
    args = parser.parse_args()
    if args.command == 'collect':
        result = collect(args.day)
    elif args.command == 'retry-sources':
        result = retry_sources(args.run)
    elif args.command == 'finish':
        result = finish(args.run, args.decisions, send_email=not args.no_email)
    else:
        with locked():
            state = ensure_wiki()
            result = {k: state[k] for k in ('directory_url', 'source_url')}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
