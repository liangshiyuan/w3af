import re
import plotille

from utils.graph import _num_formatter
from utils.utils import (get_first_timestamp,
                         get_last_timestamp,
                         get_line_epoch)

PARSER_PROCESS_MEMORY_LIMIT = re.compile('Using RLIMIT_AS memory usage limit (.*?) MB for new pool process')


def show_parser_process_memory_limit(scan):
    scan.seek(0)

    memory_limit = []
    memory_limit_timestamps = []

    for line in scan:
        match = PARSER_PROCESS_MEMORY_LIMIT.search(line)
        if match:
            memory_limit.append(int(match.group(1)))
            memory_limit_timestamps.append(get_line_epoch(line))

    first_timestamp = get_first_timestamp(scan)
    last_timestamp = get_last_timestamp(scan)
    spent_epoch = last_timestamp - first_timestamp
    memory_limit_timestamps = [ts - first_timestamp for ts in memory_limit_timestamps]

    if not memory_limit:
        print('No parser process memory limit information found')
        return

    print('Parser process memory limit')
    print('    Latest memory limit: %s MB' % memory_limit[-1])
    print('')

    fig = plotille.Figure()
    fig.width = 90
    fig.height = 20
    fig.register_label_formatter(float, _num_formatter)
    fig.register_label_formatter(int, _num_formatter)
    fig.y_label = 'Parser memory limit (MB)'
    fig.x_label = 'Time'
    fig.color_mode = 'byte'
    fig.set_x_limits(min_=0, max_=spent_epoch)
    fig.set_y_limits(min_=0, max_=max(memory_limit) * 1.1)

    fig.plot(memory_limit_timestamps,
             memory_limit,
             label='Memory limit')

    print(fig.show())
    print('')
    print('')
