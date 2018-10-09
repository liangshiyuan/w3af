import plotille

from utils.graph import _num_formatter
from utils.utils import (get_first_timestamp,
                         get_last_timestamp,
                         get_line_epoch)

PARSER_TIMEOUT = '[timeout] The parser took more than'
PARSER_MEMORY_LIMIT = 'The parser exceeded the memory usage limit of'


def show_parser_errors(scan):
    scan.seek(0)

    timeout_count = 0
    timeout_errors = []
    timeout_errors_timestamps = []

    memory_count = 0
    memory_errors = []
    memory_errors_timestamps = []

    for line in scan:
        if PARSER_TIMEOUT in line:
            timeout_count += 1
            timeout_errors.append(timeout_count)
            timeout_errors_timestamps.append(get_line_epoch(line))

        if PARSER_MEMORY_LIMIT in line:
            memory_count += 1
            memory_errors.append(memory_count)
            memory_errors_timestamps.append(get_line_epoch(line))

    first_timestamp = get_first_timestamp(scan)
    last_timestamp = get_last_timestamp(scan)
    spent_epoch = last_timestamp - first_timestamp
    timeout_errors_timestamps = [ts - first_timestamp for ts in timeout_errors_timestamps]
    memory_errors_timestamps = [ts - first_timestamp for ts in memory_errors_timestamps]

    if not memory_errors and not timeout_errors:
        print('No parser errors found')
        print('')
        return

    print('Parser errors')
    print('    Timeout errors: %s' % timeout_count)
    print('    Memory errors: %s' % memory_count)
    print('')

    fig = plotille.Figure()
    fig.width = 90
    fig.height = 20
    fig.register_label_formatter(float, _num_formatter)
    fig.register_label_formatter(int, _num_formatter)
    fig.y_label = 'Parser errors'
    fig.x_label = 'Time'
    fig.color_mode = 'byte'
    #fig.set_x_limits(min_=0, max_=spent_epoch)
    fig.set_y_limits(min_=0, max_=max(memory_count, timeout_count))

    fig.plot(timeout_errors,
             timeout_errors_timestamps,
             label='Timeout errors',
             lc=50)

    fig.plot(memory_errors,
             memory_errors_timestamps,
             label='Memory errors',
             lc=200)

    print(fig.show(legend=True))
    print('')
    print('')
