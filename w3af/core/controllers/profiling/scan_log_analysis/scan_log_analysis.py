#!/usr/bin/env python

import sys
import argparse

try:
    import plotille
except ImportError:
    print('Missing dependency, please run:\n    pip install plotille')
    sys.exit(1)

from main.main import show_scan_stats
from main.watch import watch


HELP = '''\
Usage: ./scan_log_analysis.py <scan.log>

This is a command line tool that helps identify differences in two scans.

The tool takes a scan log as input, and outputs:
 * Total scan time
 * Total time spent on grep, audit, crawl and output plugins for each scan
 * Total HTTP requests
 * Locations in the scan logs where the output was silent (no lines written in more than N seconds)

The scan log needs to have debug enabled in order for this tool to work as expected.

It is also possible to just watch one graph in the console using:

    --watch <function-name>

Where <function-name> is the name of the function in the scan_log_analysis.py file
you want to watch.
'''


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='w3af scan log analyzer', usage=HELP)

    parser.add_argument('scan_log', action='store')
    parser.add_argument('--watch', action='store', dest='watch',
                        help='Show only one graph and refresh every 5 seconds.')

    parsed_args = parser.parse_args()

    try:
        scan = file(parsed_args.scan_log)
    except:
        print('The scan log file does not exist!')
        sys.exit(2)

    if parsed_args.watch:
        watch(scan, parsed_args.watch)
    else:
        show_scan_stats(parsed_args.scan_log, scan)
