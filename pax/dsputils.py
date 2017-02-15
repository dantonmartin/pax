import numba
import numpy as np

from pax import units, exceptions
from pax.datastructure import Hit


@numba.jit(numba.int64[:](numba.from_dtype(Hit.get_dtype())[:]),
           nopython=True)
def gaps_between_hits(hits):
    """Return array of gaps between hits: a hit's 'gap' is the # of samples before that hit free of other hits.
    The gap of the first hit is 0 by definition.
    Hits should already be sorted by left boundary; we'll check this and throw an error if not.
    """
    n_hits = len(hits)
    gaps = np.zeros(n_hits, dtype=np.int64)
    if n_hits == 0:
        return gaps
    # Keep a running right boundary
    boundary = hits[0].right_central
    last_left = hits[0].left_central
    for i, hit in enumerate(hits[1:]):
        gaps[i + 1] = max(0, hit.left_central - boundary - 1)
        boundary = max(hit.right_central, boundary)
        if hit.left_central < last_left:
            raise ValueError("Hits should be sorted by central left boundary!")
        last_left = hit.left_central
    return gaps


def count_hits_per_channel(peak, config, weights=None):
    return np.bincount(peak.hits['channel'].astype(np.int16), minlength=config['n_channels'], weights=weights)


def saturation_correction(peak, channels_in_pattern, expected_pattern, confused_channels, log):
    """Return multiplicative area correction obtained by replacing area in confused_channels by
    expected area based on expected_pattern in channels_in_pattern.
    expected_pattern does not have to be normalized: we'll do that for you.
    We'll also ensure any confused_channels not in channels_in_pattern are ignored.
    """
    try:
        confused_channels = np.intersect1d(confused_channels, channels_in_pattern).astype(np.int)
    except exceptions.CoordinateOutOfRangeException:
        log.warning("Expected area fractions for peak %d-%d are zero -- "
                    "cannot compute saturation & zombie correction!" % (peak.left, peak.right))
        return 1
    # PatternFitter should have normalized the pattern
    assert abs(np.sum(expected_pattern) - 1) < 0.01

    area_seen_in_pattern = peak.area_per_channel[channels_in_pattern].sum()
    area_in_good_channels = area_seen_in_pattern - peak.area_per_channel[confused_channels].sum()
    fraction_of_pattern_in_good_channels = 1 - expected_pattern[confused_channels].sum()

    # Area in channels not in channels_in_pattern is left alone
    new_area = peak.area - area_seen_in_pattern

    # Estimate the area in channels_in_pattern by excluding the confused channels
    new_area += area_in_good_channels / fraction_of_pattern_in_good_channels

    return new_area / peak.area


def adc_to_pe(config, channel, use_reference_gain=False, use_reference_gain_if_zero=False):
    """Gives the conversion factor from ADC counts above baseline to pe/bin
    Use as: w_in_pe_bin = adc_to_pe(config, channel) * w_in_adc_above_baseline
      - config should be a configuration dictionary (self.config in a pax plugin)
      - If use_reference_gain is True, will always use config.get('pmt_reference_gain', 2e6) rather than the pmt gain
      - If use_reference_gain_if_zero=True will do the above only if channel gain is 0.
    If neither of these are true, and gain is 0, will return 0.
    """
    c = config
    adc_to_e = c['sample_duration'] * c['digitizer_voltage_range'] / (
        2 ** (c['digitizer_bits']) *
        c['pmt_circuit_load_resistor'] *
        c['external_amplification'] *
        units.electron_charge)
    try:
        pmt_gain = c['gains'][channel]
    except IndexError:
        print("Attempt to request gain for channel %d, only %d channels known. "
              "Returning reference gain instead." % (channel, len(c['gains'])))
        return c.get('pmt_reference_gain', 2e6)
    if use_reference_gain_if_zero and pmt_gain == 0 or use_reference_gain:
        pmt_gain = c.get('pmt_reference_gain', 2e6)
    if pmt_gain == 0:
        return 0
    return adc_to_e / pmt_gain


def get_detector_by_channel(config):
    """Return a channel -> detector lookup dictionary from a configuration"""
    detector_by_channel = {}
    for name, chs in config['channels_in_detector'].items():
        for ch in chs:
            detector_by_channel[ch] = name
    return detector_by_channel


@numba.jit(numba.void(numba.float64[:], numba.int64[:, :], numba.int64, numba.int64),
           nopython=True)
def extend_intervals(w, intervals, left_extension, right_extension):
    """Extends intervals on w by left_extension to left and right_extension to right, never exceeding w's bounds

    :param w: Waveform intervals live on. Only used for edges (kind of pointless to pass...)
    :param intervals: numpy N*2 array of ints of interval bounds
    :param left_extension: Extend intervals left by this number of samples,
                           or as far as possible until the end of another interval / the end of w.
    :param right_extension: Same, extend to right.
    :return: None, modifes intervals in place

    When two intervals' extension claims compete, right extension has priority.

    Boundary indices are inclusive, i.e. without any extension settings, the right boundary is the last index
    which was still above low_threshold
    """
    n_intervals = len(intervals)
    last_index_in_w = len(w) - 1

    # Right extension
    if right_extension != 0:
        for i in range(n_intervals):
            if i == n_intervals - 1:
                max_possible_r = last_index_in_w
            else:
                max_possible_r = intervals[i + 1][0] - 1
            intervals[i][1] = min(max_possible_r, intervals[i][1] + right_extension)

    # Left extension
    if left_extension != 0:
        for i in range(n_intervals):
            if i == 0:
                min_possible_l = 0
            else:
                min_possible_l = intervals[i - 1][1] + 1
            intervals[i][0] = max(min_possible_l, intervals[i][0] - left_extension)


@numba.jit(numba.int32(numba.float64[:], numba.float64, numba.int64[:, :]),
           nopython=True)
def find_intervals_above_threshold(w, threshold, result_buffer):
    """Fills result_buffer with l, r bounds of intervals in w > threshold.
    :param w: Waveform to do hitfinding in
    :param threshold: Threshold for including an interval
    :param result_buffer: numpy N*2 array of ints, will be filled by function.
                          if more than N intervals are found, none past the first N will be processed.
    :returns : number of intervals processed

    Boundary indices are inclusive, i.e. the right boundary is the last index which was > threshold
    """
    result_buffer_size = len(result_buffer)
    last_index_in_w = len(w) - 1

    in_interval = False
    current_interval = 0
    current_interval_start = -1

    for i, x in enumerate(w):

        if not in_interval and x > threshold:
            # Start of an interval
            in_interval = True
            current_interval_start = i

        if in_interval and (x <= threshold or i == last_index_in_w):
            # End of the current interval
            in_interval = False

            # The interval ended just before this index
            # Unless we ended ONLY because this is the last index, then the interval ends right here
            itv_end = i - 1 if x <= threshold else i

            # Add bounds to result buffer
            result_buffer[current_interval, 0] = current_interval_start
            result_buffer[current_interval, 1] = itv_end
            current_interval += 1

            if current_interval == result_buffer_size:
                break

    n_intervals = current_interval      # No +1, as current_interval was incremented also when the last interval closed
    return n_intervals


@numba.jit(nopython=True)
def find_split_points(w, min_height, min_ratio):
    """"Yield indices of local minima in w, whose local maxima to the left and right both satisfy:
      - larger than minimum + min_height
      - larger than minimum * min_ratio
    """
    last_max = 0
    min_since_max = 1e12        # Numba doesn't like float('inf')...
    min_since_max_i = 0

    for i, x in enumerate(w):
        if x < min_since_max:
            # New minimum since last max
            min_since_max = x
            min_since_max_i = i

        if min(last_max, x) > max(min_since_max + min_height,
                                  min_since_max * min_ratio):
            # Significant local minimum: tell caller, reset both max and min finder
            yield min_since_max_i
            last_max = x
            min_since_max = 1e12
            min_since_max_i = i

        if x > last_max:
            # New max, reset minimum finder state
            # Notice this is AFTER the split check, to accomodate very fast rising second peaks
            last_max = x
            min_since_max = 1e12
            min_since_max_i = i


@numba.jit(numba.void(numba.float64[:], numba.int64[:, :],
                      numba.from_dtype(Hit.get_dtype())[:],
                      numba.float64, numba.int64, numba.float64, numba.int64, numba.int64, numba.int64, numba.float64,
                      numba.int64[:, :]),
           nopython=True)
def build_hits(w, hit_bounds,
               hits_buffer,
               adc_to_pe, channel, noise_sigma_pe, dt, start, pulse_i, saturation_threshold, central_bounds):
    """Populates hits_buffer with properties from hits indicated by hit_bounds.
        hit_bounds should be a numpy array of (left, right) bounds (inclusive) in w
    Returns nothing.
    """
    for hit_i in range(len(hit_bounds)):
        amplitude = -999.9
        argmax = -1
        area = 0.0
        center = 0.0
        deviation = 0.0
        saturation_count = 0
        left = hit_bounds[hit_i, 0]
        right = hit_bounds[hit_i, 1]
        for i, x in enumerate(w[left:right + 1]):
            if x > amplitude:
                amplitude = x
                argmax = i
            if x > saturation_threshold:
                saturation_count += 1
            area += x
            center += x * i

        # During gain calibration, or if the low threshold is set to negative values,
        # the hitfinder can include regions with negative amplitudes
        # In rare cases this can make the area come out at 0, in which case this code
        # would throw a divide by zero exception.
        if area != 0:
            center /= area
            for i, x in enumerate(w[left:right + 1]):
                deviation += x * abs(i - center)
            deviation /= area

        # Store the hit properties
        hits_buffer[hit_i].channel = channel
        hits_buffer[hit_i].found_in_pulse = pulse_i
        hits_buffer[hit_i].noise_sigma = noise_sigma_pe
        hits_buffer[hit_i].left = left + start
        hits_buffer[hit_i].right = right + start
        hits_buffer[hit_i].left_central = central_bounds[hit_i, 0] + start
        hits_buffer[hit_i].right_central = central_bounds[hit_i, 1] + start
        hits_buffer[hit_i].sum_absolute_deviation = deviation
        hits_buffer[hit_i].center = (start + left + center) * dt
        hits_buffer[hit_i].index_of_maximum = start + left + argmax
        hits_buffer[hit_i].n_saturated = saturation_count

        # In certain pathological cases (e.g. due to splitting hits later in LocalMinimumClustering)
        # hits can have negative area or (even rarer) negative height.
        # This leads to problems in later code, so we force a minimum area and height of 1e-9)
        hits_buffer[hit_i].area = max(1e-9, area * adc_to_pe)
        hits_buffer[hit_i].height = max(1e-9, w[argmax + left] * adc_to_pe)
