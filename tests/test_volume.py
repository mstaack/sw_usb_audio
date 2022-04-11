# Copyright (c) 2022, XMOS Ltd, All rights reserved
import io
from pathlib import Path
import pytest
import time
import re
import json
import subprocess
import re
import tempfile

from usb_audio_test_utils import (wait_for_portaudio, get_firmware_path_harness,
    get_firmware_path, run_audio_command, mark_tests)


class Volcontrol:
    EXECUTABLE = Path(__file__).parent / "tools" / "volcontrol" / "volcontrol"

    def __init__(self, input_output, num_chans, channel=None, master=False):
        self.channel = '0' if master else f'{channel + 1}'
        self.reset_chans = f'{num_chans + 1}'
        self.input_output = input_output

    def reset(self):
        subprocess.run([self.EXECUTABLE, '--resetall', self.reset_chans], check=True)
        # sleep after resetting to allow the analyzer to detect the change
        time.sleep(3)

    def set(self, value):
        subprocess.run([self.EXECUTABLE, '--set', self.input_output, self.channel, f'{value}'],
                       check=True)
        # sleep after setting the volume to allow the analyzer to detect the change
        time.sleep(3)


def get_line_matches(lines, expected):
    matches = []
    for line in lines:
        match = re.search(expected, line)
        if match:
            matches.append(match.group(1))

    return matches


def check_analyzer_output(analyzer_output, xsig_config):
    """ Verify that the output from xsig is correct """

    failures = []
    # Check for any errors
    for line in analyzer_output:
        if re.match(".*ERROR|.*error|.*Error|.*Problem", line):
            failures.append(line)

    num_chans = len(xsig_config)
    analyzer_channels = [[] for _ in range(num_chans)]
    for line in analyzer_output:
        match = re.search(r'^Channel (\d+):', line)
        if not match:
            continue

        channel = int(match.group(1))
        if channel not in range(num_chans):
            failures.append(f'Invalid channel number {channel}')
            continue

        analyzer_channels[channel].append(line)

        if re.match(r'Channel \d+: Lost signal', line):
            failures.append(line)

    for idx, channel_config in enumerate(xsig_config):
        if channel_config[0] == 'volcheck':
            vol_changes = get_line_matches(analyzer_channels[idx], r'.*Volume change by (-?\d+)')

            if len(vol_changes) < 2:
                failures.append(f'Initial volume and initial change not found on channel {idx}')
                continue

            initial_volume = int(vol_changes.pop(0))
            initial_change = int(vol_changes.pop(0))
            if initial_change >= 0:
                failures.append(f'Initial change is not negative on channel {idx}: {initial_change}')
            initial_change = abs(initial_change)
            exp_vol_changes = [1.0, -0.5, 0.5]
            if len(vol_changes) != len(exp_vol_changes):
                failures.append(f'Unexpected number of volume changes on channel {idx}: {vol_changes}')
                continue

            for vol_change, exp_ratio in zip(vol_changes, exp_vol_changes):
                expected = initial_change * exp_ratio
                if abs(int(vol_change) - expected) > 2:
                    failures.append(f'Volume change not as expected on channel {idx}: actual {vol_change}, expected {expected}')

        elif channel_config[0] == 'sine':
            exp_freq = channel_config[1]
            chan_freqs = get_line_matches(analyzer_channels[idx], r'^Channel \d+: Frequency (\d+)')
            if not len(chan_freqs):
                failures.append(f'No signal seen on channel {idx}')
            for freq in chan_freqs:
                if int(freq) != exp_freq:
                    failures.append(f'Incorrect frequency on channel {idx}; got {freq}, expected {exp_freq}')
        else:
            failures.append(f'Invalid channel config {channel_config}')

    if len(failures) > 0:
        pytest.fail('Checking analyser output failed:\n' + '\n'.join(failures))


# Test cases are defined by a tuple of (board, config, sample rate, 'm' (master) or channel number)
volume_input_configs = [
    # smoke level tests
    *mark_tests(pytest.mark.smoke, [
        *[('xk_216_mc',    '2i10o10xxxxxx',        96000, ch) for ch in ['m', *range(8)]],
        *[('xk_evk_xu316', '2i2o2',                48000, ch) for ch in ['m', *range(2)]]
    ]),

    # nightly level tests
    *mark_tests(pytest.mark.nightly, [
        *[('xk_216_mc',    '2i8o8xxxxx_tdm8',      48000, ch) for ch in ['m', *range(8)]],
        *[('xk_216_mc',    '2i10o10msxxxx',       192000, ch) for ch in ['m', *range(8)]],
        *[('xk_evk_xu316', '2i2o2',                44100, ch) for ch in ['m', *range(2)]],
        *[('xk_evk_xu316', '2i2o2',                96000, ch) for ch in ['m', *range(2)]]
    ]),

    # weekend level tests
    *mark_tests(pytest.mark.weekend, [
        *[('xk_216_mc',    '2i10o10xsxxxx_mix8',   44100, ch) for ch in ['m', *range(8)]],
        *[('xk_216_mc',    '2i10o10xssxxx',       176400, ch) for ch in ['m', *range(8)]],
        *[('xk_evk_xu316', '2i2o2',                88200, ch) for ch in ['m', *range(2)]],
        *[('xk_evk_xu316', '2i2o2',               176400, ch) for ch in ['m', *range(2)]],
        *[('xk_evk_xu316', '2i2o2',               192000, ch) for ch in ['m', *range(2)]]
    ])
]


@pytest.mark.parametrize(["board", "config", "fs", "channel"], volume_input_configs)
def test_volume_input(xtagctl_wrapper, xsig, board, config, fs, channel):
    if board == "xk_216_mc":
        num_chans = 8
    elif board == "xk_evk_xu316":
        num_chans = 2
    else:
        pytest.fail(f'Unrecognised board {board}')

    channels = range(num_chans) if channel == "m" else [channel]

    duration = 25

    # Load JSON xsig_config data
    xsig_config = f'mc_analogue_input_{num_chans}ch.json'
    xsig_config_path = Path(__file__).parent / "xsig_configs" / xsig_config
    with open(xsig_config_path) as file:
        xsig_json = json.load(file)

    for ch, ch_config in enumerate(xsig_json["in"]):
        if ch in channels:
            xsig_json["in"][ch][0] = "volcheck"

    adapter_dut, adapter_harness = xtagctl_wrapper

    # xrun the harness
    harness_firmware = get_firmware_path_harness("xcore200_mc")
    subprocess.run(['xrun', '--adapter-id', adapter_harness, harness_firmware], check=True)
    # xflash the firmware
    firmware = get_firmware_path(board, config)
    subprocess.run(['xrun', '--adapter-id', adapter_dut, firmware], check=True)

    wait_for_portaudio(board, config)

    with tempfile.NamedTemporaryFile(mode='w+') as out_file, tempfile.NamedTemporaryFile(mode='w') as xsig_file:
        json.dump(xsig_json, xsig_file)
        xsig_file.flush()

        run_audio_command(out_file, xsig, f"{fs}", f"{duration * 1000}", Path(xsig_file.name))

        time.sleep(5)

        if channel == 'm':
            vol_in = Volcontrol('input', num_chans, master=True)
        else:
            vol_in = Volcontrol('input', num_chans, channel=int(channel))

        vol_in.reset()
        vol_changes = [0.5, 1.0, 0.75, 1.0]
        for vol_change in vol_changes:
            vol_in.set(vol_change)

        out_file.seek(0)
        xsig_lines = out_file.readlines()

    # Check output
    check_analyzer_output(xsig_lines, xsig_json['in'])
