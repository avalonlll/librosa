#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Temporal segmentation
=====================

Recurrence and self-similarity
------------------------------
.. autosummary::
    :toctree: generated/

    recurrence_matrix
    recurrence_to_lag
    lag_to_recurrence
    timelag_filter
    path_enhance

Temporal clustering
-------------------
.. autosummary::
    :toctree: generated/

    agglomerative
    subsegment
"""

from decorator import decorator

import numpy as np
import scipy
import scipy.signal
import scipy.ndimage

import sklearn
import sklearn.cluster
import sklearn.feature_extraction
import sklearn.neighbors

from ._cache import cache
from . import util
from .filters import diagonal_filter
from .util.exceptions import ParameterError

__all__ = ['recurrence_matrix',
           'recurrence_to_lag',
           'lag_to_recurrence',
           'timelag_filter',
           'agglomerative',
           'subsegment',
           'path_enhance']


@cache(level=30)
def recurrence_matrix(data, k=None, width=1, metric='euclidean',
                      sym=False, sparse=False, mode='connectivity',
                      bandwidth=None, self=False, axis=-1):
    '''Compute a recurrence matrix from a data matrix.

    `rec[i, j]` is non-zero if (`data[:, i]`, `data[:, j]`) are
    k-nearest-neighbors and `|i - j| >= width`

    The specific value of `rec[i, j]` can have several forms, governed
    by the `mode` parameter below:

        - Connectivity: `rec[i, j] = 1 or 0` indicates that frames `i` and `j` are repetitions

        - Affinity: `rec[i, j] > 0` measures how similar frames `i` and `j` are.  This is also
          known as a (sparse) self-similarity matrix.

        - Distance: `rec[, j] > 0` measures how distant frames `i` and `j` are.  This is also
          known as a (sparse) self-distance matrix.

    The general term *recurrence matrix* can refer to any of the three forms above.


    Parameters
    ----------
    data : np.ndarray
        A feature matrix

    k : int > 0 [scalar] or None
        the number of nearest-neighbors for each sample

        Default: `k = 2 * ceil(sqrt(t - 2 * width + 1))`,
        or `k = 2` if `t <= 2 * width + 1`

    width : int >= 1 [scalar]
        only link neighbors `(data[:, i], data[:, j])`
        if `|i - j| >= width`

        `width` cannot exceed the length of the data.

    metric : str
        Distance metric to use for nearest-neighbor calculation.

        See `sklearn.neighbors.NearestNeighbors` for details.

    sym : bool [scalar]
        set `sym=True` to only link mutual nearest-neighbors

    sparse : bool [scalar]
        if False, returns a dense type (ndarray)
        if True, returns a sparse type (scipy.sparse.csr_matrix)

    mode : str, {'connectivity', 'distance', 'affinity'}
        If 'connectivity', a binary connectivity matrix is produced.

        If 'distance', then a non-zero entry contains the distance between
        points.

        If 'affinity', then non-zero entries are mapped to
        `exp( - distance(i, j) / bandwidth)` where `bandwidth` is
        as specified below.

    bandwidth : None or float > 0
        If using ``mode='affinity'``, this can be used to set the
        bandwidth on the affinity kernel.

        If no value is provided, it is set automatically to the median
        distance between furthest nearest neighbors.

    self : bool
        If `True`, then the main diagonal is populated with self-links:
        0 if ``mode='distance'``, and 1 otherwise.

        If `False`, the main diagonal is left empty.

    axis : int
        The axis along which to compute recurrence.
        By default, the last index (-1) is taken.

    Returns
    -------
    rec : np.ndarray or scipy.sparse.csr_matrix, [shape=(t, t)]
        Recurrence matrix

    See Also
    --------
    sklearn.neighbors.NearestNeighbors
    scipy.spatial.distance.cdist
    librosa.feature.stack_memory
    recurrence_to_lag

    Notes
    -----
    This function caches at level 30.

    Examples
    --------
    Find nearest neighbors in MFCC space

    >>> y, sr = librosa.load(librosa.util.example_audio_file())
    >>> mfcc = librosa.feature.mfcc(y=y, sr=sr)
    >>> R = librosa.segment.recurrence_matrix(mfcc)

    Or fix the number of nearest neighbors to 5

    >>> R = librosa.segment.recurrence_matrix(mfcc, k=5)

    Suppress neighbors within +- 7 samples

    >>> R = librosa.segment.recurrence_matrix(mfcc, width=7)

    Use cosine similarity instead of Euclidean distance

    >>> R = librosa.segment.recurrence_matrix(mfcc, metric='cosine')

    Require mutual nearest neighbors

    >>> R = librosa.segment.recurrence_matrix(mfcc, sym=True)

    Use an affinity matrix instead of binary connectivity

    >>> R_aff = librosa.segment.recurrence_matrix(mfcc, mode='affinity')

    Plot the feature and recurrence matrices

    >>> import matplotlib.pyplot as plt
    >>> plt.figure(figsize=(8, 4))
    >>> plt.subplot(1, 2, 1)
    >>> librosa.display.specshow(R, x_axis='time', y_axis='time')
    >>> plt.title('Binary recurrence (symmetric)')
    >>> plt.subplot(1, 2, 2)
    >>> librosa.display.specshow(R_aff, x_axis='time', y_axis='time',
    ...                          cmap='magma_r')
    >>> plt.title('Affinity recurrence')
    >>> plt.tight_layout()
    >>> plt.show()

    '''

    data = np.atleast_2d(data)

    # Swap observations to the first dimension and flatten the rest
    data = np.swapaxes(data, axis, 0)
    t = data.shape[0]
    data = data.reshape((t, -1))

    if width < 1 or width > t:
        raise ParameterError('width={} must be at least 1 and at most data.shape[{}]={}'.format(width, axis, t))

    if mode not in ['connectivity', 'distance', 'affinity']:
        raise ParameterError(("Invalid mode='{}'. Must be one of "
                              "['connectivity', 'distance', "
                              "'affinity']").format(mode))
    if k is None:
        if t > 2 * width + 1:
            k = 2 * np.ceil(np.sqrt(t - 2 * width + 1))
        else:
            k = 2

    if bandwidth is not None:
        if bandwidth <= 0:
            raise ParameterError('Invalid bandwidth={}. '
                                 'Must be strictly positive.'.format(bandwidth))

    k = int(k)

    # Build the neighbor search object
    try:
        knn = sklearn.neighbors.NearestNeighbors(n_neighbors=min(t-1, k + 2 * width),
                                                 metric=metric,
                                                 algorithm='auto')
    except ValueError:
        knn = sklearn.neighbors.NearestNeighbors(n_neighbors=min(t-1, k + 2 * width),
                                                 metric=metric,
                                                 algorithm='brute')

    knn.fit(data)

    # Get the knn graph
    if mode == 'affinity':
        kng_mode = 'distance'
    else:
        kng_mode = mode

    rec = knn.kneighbors_graph(mode=kng_mode).tolil()

    # Remove connections within width
    for diag in range(-width + 1, width):
        rec.setdiag(0, diag)

    # Retain only the top-k links per point
    for i in range(t):
        # Get the links from point i
        links = rec[i].nonzero()[1]

        # Order them ascending
        idx = links[np.argsort(rec[i, links].toarray())][0]

        # Everything past the kth closest gets squashed
        rec[i, idx[k:]] = 0

    if self:
        if mode == 'connectivity':
            rec.setdiag(1)
        elif mode == 'affinity':
            # we need to keep the self-loop in here, but not mess up the
            # bandwidth estimation
            #
            # using negative distances here preserves the structure without changing
            # the statistics of the data
            rec.setdiag(-1)

    # symmetrize
    if sym:
        # Note: this operation produces a CSR (compressed sparse row) matrix!
        # This is why we have to do it after filling the diagonal in self-mode
        rec = rec.minimum(rec.T)

    rec = rec.tocsr()
    rec.eliminate_zeros()

    if mode == 'connectivity':
        rec = rec.astype(np.bool)
    elif mode == 'affinity':
        if bandwidth is None:
            bandwidth = np.nanmedian(rec.max(axis=1).data)
        # Set all the negatives back to 0
        # Negatives are temporarily inserted above to preserve the sparsity structure
        # of the matrix without corrupting the bandwidth calculations
        rec.data[rec.data < 0] = 0.0
        rec.data[:] = np.exp(rec.data / (-1 * bandwidth))

    if not sparse:
        rec = rec.toarray()

    return rec


def recurrence_to_lag(rec, pad=True, axis=-1):
    '''Convert a recurrence matrix into a lag matrix.

        `lag[i, j] == rec[i+j, j]`

    Parameters
    ----------
    rec : np.ndarray, or scipy.sparse.spmatrix [shape=(n, n)]
        A (binary) recurrence matrix, as returned by `recurrence_matrix`

    pad : bool
        If False, `lag` matrix is square, which is equivalent to
        assuming that the signal repeats itself indefinitely.

        If True, `lag` is padded with `n` zeros, which eliminates
        the assumption of repetition.

    axis : int
        The axis to keep as the `time` axis.
        The alternate axis will be converted to lag coordinates.

    Returns
    -------
    lag : np.ndarray
        The recurrence matrix in (lag, time) (if `axis=1`)
        or (time, lag) (if `axis=0`) coordinates

    Raises
    ------
    ParameterError : if `rec` is non-square

    See Also
    --------
    recurrence_matrix
    lag_to_recurrence

    Examples
    --------
    >>> y, sr = librosa.load(librosa.util.example_audio_file())
    >>> mfccs = librosa.feature.mfcc(y=y, sr=sr)
    >>> recurrence = librosa.segment.recurrence_matrix(mfccs)
    >>> lag_pad = librosa.segment.recurrence_to_lag(recurrence, pad=True)
    >>> lag_nopad = librosa.segment.recurrence_to_lag(recurrence, pad=False)

    >>> import matplotlib.pyplot as plt
    >>> plt.figure(figsize=(8, 4))
    >>> plt.subplot(1, 2, 1)
    >>> librosa.display.specshow(lag_pad, x_axis='time', y_axis='lag')
    >>> plt.title('Lag (zero-padded)')
    >>> plt.subplot(1, 2, 2)
    >>> librosa.display.specshow(lag_nopad, x_axis='time')
    >>> plt.title('Lag (no padding)')
    >>> plt.tight_layout()
    >>> plt.show()
    '''

    axis = np.abs(axis)

    if rec.ndim != 2 or rec.shape[0] != rec.shape[1]:
        raise ParameterError('non-square recurrence matrix shape: '
                             '{}'.format(rec.shape))

    sparse = scipy.sparse.issparse(rec)

    roll_ax = None
    if sparse:
        roll_ax = 1 - axis
        lag_format = rec.format
        if axis == 0:
            rec = rec.tocsc()
        elif axis in (-1, 1):
            rec = rec.tocsr()

    t = rec.shape[axis]

    if sparse:
        if pad:
            kron = np.asarray([[1, 0]]).swapaxes(axis, 0)
            lag = scipy.sparse.kron(kron.astype(rec.dtype), rec, format='lil')
        else:
            lag = scipy.sparse.lil_matrix(rec)
    else:
        if pad:
            padding = [(0, 0), (0, 0)]
            padding[(1-axis)] = (0, t)
            lag = np.pad(rec, padding, mode='constant')
        else:
            lag = rec.copy()

    idx_slice = [slice(None)] * lag.ndim

    for i in range(1, t):
        idx_slice[axis] = i
        lag[tuple(idx_slice)] = util.roll_sparse(lag[tuple(idx_slice)], -i, axis=roll_ax)

    if sparse:
        return lag.asformat(lag_format)
    return np.ascontiguousarray(lag.T).T


def lag_to_recurrence(lag, axis=-1):
    '''Convert a lag matrix into a recurrence matrix.

    Parameters
    ----------
    lag : np.ndarray or scipy.sparse.spmatrix
        A lag matrix, as produced by `recurrence_to_lag`

    axis : int
        The axis corresponding to the time dimension.
        The alternate axis will be interpreted in lag coordinates.

    Returns
    -------
    rec : np.ndarray or scipy.sparse.spmatrix [shape=(n, n)]
        A recurrence matrix in (time, time) coordinates
        For sparse matrices, format will match that of `lag`.

    Raises
    ------
    ParameterError : if `lag` does not have the correct shape

    See Also
    --------
    recurrence_to_lag

    Examples
    --------
    >>> y, sr = librosa.load(librosa.util.example_audio_file())
    >>> mfccs = librosa.feature.mfcc(y=y, sr=sr)
    >>> recurrence = librosa.segment.recurrence_matrix(mfccs)
    >>> lag_pad = librosa.segment.recurrence_to_lag(recurrence, pad=True)
    >>> lag_nopad = librosa.segment.recurrence_to_lag(recurrence, pad=False)
    >>> rec_pad = librosa.segment.lag_to_recurrence(lag_pad)
    >>> rec_nopad = librosa.segment.lag_to_recurrence(lag_nopad)

    >>> import matplotlib.pyplot as plt
    >>> plt.figure(figsize=(8, 4))
    >>> plt.subplot(2, 2, 1)
    >>> librosa.display.specshow(lag_pad, x_axis='time', y_axis='lag')
    >>> plt.title('Lag (zero-padded)')
    >>> plt.subplot(2, 2, 2)
    >>> librosa.display.specshow(lag_nopad, x_axis='time', y_axis='time')
    >>> plt.title('Lag (no padding)')
    >>> plt.subplot(2, 2, 3)
    >>> librosa.display.specshow(rec_pad, x_axis='time', y_axis='time')
    >>> plt.title('Recurrence (with padding)')
    >>> plt.subplot(2, 2, 4)
    >>> librosa.display.specshow(rec_nopad, x_axis='time', y_axis='time')
    >>> plt.title('Recurrence (without padding)')
    >>> plt.tight_layout()
    >>> plt.show()

    '''

    if axis not in [0, 1, -1]:
        raise ParameterError('Invalid target axis: {}'.format(axis))

    axis = np.abs(axis)

    if lag.ndim != 2 or (lag.shape[0] != lag.shape[1] and
                         lag.shape[1 - axis] != 2 * lag.shape[axis]):
        raise ParameterError('Invalid lag matrix shape: {}'.format(lag.shape))

    # Since lag must be 2-dimensional, abs(axis) = axis
    t = lag.shape[axis]

    sparse = scipy.sparse.issparse(lag)
    if sparse:
        rec = scipy.sparse.lil_matrix(lag)
        roll_ax = 1 - axis
    else:
        rec = lag.copy()
        roll_ax = None

    idx_slice = [slice(None)] * lag.ndim
    for i in range(1, t):
        idx_slice[axis] = i
        rec[tuple(idx_slice)] = util.roll_sparse(lag[tuple(idx_slice)], i, axis=roll_ax)

    sub_slice = [slice(None)] * rec.ndim
    sub_slice[1 - axis] = slice(t)
    rec = rec[tuple(sub_slice)]

    if sparse:
        return rec.asformat(lag.format)
    return np.ascontiguousarray(rec.T).T


def timelag_filter(function, pad=True, index=0):
    '''Filtering in the time-lag domain.

    This is primarily useful for adapting image filters to operate on
    `recurrence_to_lag` output.

    Using `timelag_filter` is equivalent to the following sequence of
    operations:

    >>> data_tl = librosa.segment.recurrence_to_lag(data)
    >>> data_filtered_tl = function(data_tl)
    >>> data_filtered = librosa.segment.lag_to_recurrence(data_filtered_tl)

    Parameters
    ----------
    function : callable
        The filtering function to wrap, e.g., `scipy.ndimage.median_filter`

    pad : bool
        Whether to zero-pad the structure feature matrix

    index : int >= 0
        If `function` accepts input data as a positional argument, it should be
        indexed by `index`


    Returns
    -------
    wrapped_function : callable
        A new filter function which applies in time-lag space rather than
        time-time space.


    Examples
    --------

    Apply a 5-bin median filter to the diagonal of a recurrence matrix

    >>> y, sr = librosa.load(librosa.util.example_audio_file())
    >>> chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    >>> rec = librosa.segment.recurrence_matrix(chroma)
    >>> from scipy.ndimage import median_filter
    >>> diagonal_median = librosa.segment.timelag_filter(median_filter)
    >>> rec_filtered = diagonal_median(rec, size=(1, 3), mode='mirror')

    Or with affinity weights

    >>> rec_aff = librosa.segment.recurrence_matrix(chroma, mode='affinity')
    >>> rec_aff_fil = diagonal_median(rec_aff, size=(1, 3), mode='mirror')

    >>> import matplotlib.pyplot as plt
    >>> plt.figure(figsize=(8,8))
    >>> plt.subplot(2, 2, 1)
    >>> librosa.display.specshow(rec, y_axis='time')
    >>> plt.title('Raw recurrence matrix')
    >>> plt.subplot(2, 2, 2)
    >>> librosa.display.specshow(rec_filtered)
    >>> plt.title('Filtered recurrence matrix')
    >>> plt.subplot(2, 2, 3)
    >>> librosa.display.specshow(rec_aff, x_axis='time', y_axis='time',
    ...                          cmap='magma_r')
    >>> plt.title('Raw affinity matrix')
    >>> plt.subplot(2, 2, 4)
    >>> librosa.display.specshow(rec_aff_fil, x_axis='time',
    ...                          cmap='magma_r')
    >>> plt.title('Filtered affinity matrix')
    >>> plt.tight_layout()
    >>> plt.show()
    '''

    def __my_filter(wrapped_f, *args, **kwargs):
        '''Decorator to wrap the filter'''
        # Map the input data into time-lag space
        args = list(args)

        args[index] = recurrence_to_lag(args[index], pad=pad)

        # Apply the filtering function
        result = wrapped_f(*args, **kwargs)

        # Map back into time-time and return
        return lag_to_recurrence(result)

    return decorator(__my_filter, function)


@cache(level=30)
def subsegment(data, frames, n_segments=4, axis=-1):
    '''Sub-divide a segmentation by feature clustering.

    Given a set of frame boundaries (`frames`), and a data matrix (`data`),
    each successive interval defined by `frames` is partitioned into
    `n_segments` by constrained agglomerative clustering.

    .. note::
        If an interval spans fewer than `n_segments` frames, then each
        frame becomes a sub-segment.

    Parameters
    ----------
    data : np.ndarray
        Data matrix to use in clustering

    frames : np.ndarray [shape=(n_boundaries,)], dtype=int, non-negative]
        Array of beat or segment boundaries, as provided by
        `librosa.beat.beat_track`,
        `librosa.onset.onset_detect`,
        or `agglomerative`.

    n_segments : int > 0
        Maximum number of frames to sub-divide each interval.

    axis : int
        Axis along which to apply the segmentation.
        By default, the last index (-1) is taken.

    Returns
    -------
    boundaries : np.ndarray [shape=(n_subboundaries,)]
        List of sub-divided segment boundaries

    See Also
    --------
    agglomerative : Temporal segmentation
    librosa.onset.onset_detect : Onset detection
    librosa.beat.beat_track : Beat tracking

    Notes
    -----
    This function caches at level 30.

    Examples
    --------
    Load audio, detect beat frames, and subdivide in twos by CQT

    >>> y, sr = librosa.load(librosa.util.example_audio_file(), duration=8)
    >>> tempo, beats = librosa.beat.beat_track(y=y, sr=sr, hop_length=512)
    >>> beat_times = librosa.frames_to_time(beats, sr=sr, hop_length=512)
    >>> cqt = np.abs(librosa.cqt(y, sr=sr, hop_length=512))
    >>> subseg = librosa.segment.subsegment(cqt, beats, n_segments=2)
    >>> subseg_t = librosa.frames_to_time(subseg, sr=sr, hop_length=512)
    >>> subseg
    array([  0,   2,   4,  21,  23,  26,  43,  55,  63,  72,  83,
            97, 102, 111, 122, 137, 142, 153, 162, 180, 182, 185,
           202, 210, 221, 231, 241, 256, 261, 271, 281, 296, 301,
           310, 320, 339, 341, 344, 361, 368, 382, 389, 401, 416,
           420, 430, 436, 451, 456, 465, 476, 489, 496, 503, 515,
           527, 535, 544, 553, 558, 571, 578, 590, 607, 609, 638])

    >>> import matplotlib.pyplot as plt
    >>> plt.figure()
    >>> librosa.display.specshow(librosa.amplitude_to_db(cqt,
    ...                                                  ref=np.max),
    ...                          y_axis='cqt_hz', x_axis='time')
    >>> lims = plt.gca().get_ylim()
    >>> plt.vlines(beat_times, lims[0], lims[1], color='lime', alpha=0.9,
    ...            linewidth=2, label='Beats')
    >>> plt.vlines(subseg_t, lims[0], lims[1], color='linen', linestyle='--',
    ...            linewidth=1.5, alpha=0.5, label='Sub-beats')
    >>> plt.legend(frameon=True, shadow=True)
    >>> plt.title('CQT + Beat and sub-beat markers')
    >>> plt.tight_layout()
    >>> plt.show()

    '''

    frames = util.fix_frames(frames, x_min=0, x_max=data.shape[axis], pad=True)

    if n_segments < 1:
        raise ParameterError('n_segments must be a positive integer')

    boundaries = []
    idx_slices = [slice(None)] * data.ndim

    for seg_start, seg_end in zip(frames[:-1], frames[1:]):
        idx_slices[axis] = slice(seg_start, seg_end)
        boundaries.extend(seg_start + agglomerative(data[tuple(idx_slices)],
                                                    min(seg_end - seg_start, n_segments),
                                                    axis=axis))

    return np.ascontiguousarray(boundaries)


def agglomerative(data, k, clusterer=None, axis=-1):
    """Bottom-up temporal segmentation.

    Use a temporally-constrained agglomerative clustering routine to partition
    `data` into `k` contiguous segments.

    Parameters
    ----------
    data     : np.ndarray
        data to cluster

    k        : int > 0 [scalar]
        number of segments to produce

    clusterer : sklearn.cluster.AgglomerativeClustering, optional
        An optional AgglomerativeClustering object.
        If `None`, a constrained Ward object is instantiated.

    axis : int
        axis along which to cluster.
        By default, the last axis (-1) is chosen.

    Returns
    -------
    boundaries : np.ndarray [shape=(k,)]
        left-boundaries (frame numbers) of detected segments. This
        will always include `0` as the first left-boundary.

    See Also
    --------
    sklearn.cluster.AgglomerativeClustering

    Examples
    --------
    Cluster by chroma similarity, break into 20 segments

    >>> y, sr = librosa.load(librosa.util.example_audio_file(), duration=15)
    >>> chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    >>> bounds = librosa.segment.agglomerative(chroma, 20)
    >>> bound_times = librosa.frames_to_time(bounds, sr=sr)
    >>> bound_times
    array([  0.   ,   1.672,   2.322,   2.624,   3.251,   3.506,
             4.18 ,   5.387,   6.014,   6.293,   6.943,   7.198,
             7.848,   9.033,   9.706,   9.961,  10.635,  10.89 ,
            11.54 ,  12.539])

    Plot the segmentation over the chromagram

    >>> import matplotlib.pyplot as plt
    >>> plt.figure()
    >>> librosa.display.specshow(chroma, y_axis='chroma', x_axis='time')
    >>> plt.vlines(bound_times, 0, chroma.shape[0], color='linen', linestyle='--',
    ...            linewidth=2, alpha=0.9, label='Segment boundaries')
    >>> plt.axis('tight')
    >>> plt.legend(frameon=True, shadow=True)
    >>> plt.title('Power spectrogram')
    >>> plt.tight_layout()
    >>> plt.show()

    """

    # Make sure we have at least two dimensions
    data = np.atleast_2d(data)

    # Swap data index to position 0
    data = np.swapaxes(data, axis, 0)

    # Flatten the features
    n = data.shape[0]
    data = data.reshape((n, -1))

    if clusterer is None:
        # Connect the temporal connectivity graph
        grid = sklearn.feature_extraction.image.grid_to_graph(n_x=n,
                                                              n_y=1, n_z=1)

        # Instantiate the clustering object
        clusterer = sklearn.cluster.AgglomerativeClustering(n_clusters=k,
                                                            connectivity=grid,
                                                            memory=cache.memory)

    # Fit the model
    clusterer.fit(data)

    # Find the change points from the labels
    boundaries = [0]
    boundaries.extend(
        list(1 + np.nonzero(np.diff(clusterer.labels_))[0].astype(int)))
    return np.asarray(boundaries)


def path_enhance(R, n, window='hann', max_ratio=2.0, min_ratio=None, n_filters=7,
                 zero_mean=False, clip=True, **kwargs):
    '''Multi-angle path enhancement for self- and cross-similarity matrices.

    This function convolves multiple diagonal smoothing filters with a self-similarity (or
    recurrence) matrix R, and aggregates the result by an element-wise maximum.

    Technically, the output is a matrix R_smooth such that

        `R_smooth[i, j] = max_theta (R * filter_theta)[i, j]`

    where `*` denotes 2-dimensional convolution, and `filter_theta` is a smoothing filter at
    orientation theta.

    This is intended to provide coherent temporal smoothing of self-similarity matrices
    when there are changes in tempo.

    Smoothing filters are generated at evenly spaced orientations between min_ratio and
    max_ratio.

    This function is inspired by the multi-angle path enhancement of [1]_, but differs by
    modeling tempo differences in the space of similarity matrices rather than re-sampling
    the underlying features prior to generating the self-similarity matrix.

    .. [1] Müller, Meinard and Frank Kurth.
            "Enhancing similarity matrices for music audio analysis."
            2006 IEEE International Conference on Acoustics Speech and Signal Processing Proceedings.
            Vol. 5. IEEE, 2006.

    .. note:: if using recurrence_matrix to construct the input similarity matrix, be sure to include the main
              diagonal by setting `self=True`.  Otherwise, the diagonal will be suppressed, and this is likely to
              produce discontinuities which will pollute the smoothing filter response.

    Parameters
    ----------
    R : np.ndarray
        The self- or cross-similarity matrix to be smoothed.
        Note: sparse inputs are not supported.

    n : int > 0
        The length of the smoothing filter

    window : window specification
        The type of smoothing filter to use.  See `filters.get_window` for more information
        on window specification formats.

    max_ratio : float > 0
        The maximum tempo ratio to support

    min_ratio : float > 0
        The minimum tempo ratio to support.
        If not provided, it will default to `1/max_ratio`

    n_filters : int >= 1
        The number of different smoothing filters to use, evenly spaced
        between `min_ratio` and `max_ratio`.

        If `min_ratio = 1/max_ratio` (the default), using an odd number
        of filters will ensure that the main diagonal (ratio=1) is included.

    zero_mean : bool
        By default, the smoothing filters are non-negative and sum to one (i.e. are averaging
        filters).

        If `zero_mean=True`, then the smoothing filters are made to sum to zero by subtracting
        a constant value from the non-diagonal coordinates of the filter.  This is primarily
        useful for suppressing blocks while enhancing diagonals.

    clip : bool
        If True, the smoothed similarity matrix will be thresholded at 0, and will not contain
        negative entries.

    kwargs : additional keyword arguments
        Additional arguments to pass to `scipy.ndimage.convolve`


    Returns
    -------
    R_smooth : np.ndarray, shape=R.shape
        The smoothed self- or cross-similarity matrix

    See Also
    --------
    filters.diagonal_filter
    recurrence_matrix


    Examples
    --------
    Use a 51-frame diagonal smoothing filter to enhance paths in a recurrence matrix

    >>> y, sr = librosa.load(librosa.util.example_audio_file(), duration=30)
    >>> chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    >>> rec = librosa.segment.recurrence_matrix(chroma, mode='affinity', self=True)
    >>> rec_smooth = librosa.segment.path_enhance(rec, 51, window='hann', n_filters=7)

    Plot the recurrence matrix before and after smoothing

    >>> import matplotlib.pyplot as plt
    >>> plt.figure(figsize=(8, 4))
    >>> plt.subplot(1,2,1)
    >>> librosa.display.specshow(rec, x_axis='time', y_axis='time')
    >>> plt.title('Unfiltered recurrence')
    >>> plt.subplot(1,2,2)
    >>> librosa.display.specshow(rec_smooth, x_axis='time', y_axis='time')
    >>> plt.title('Multi-angle enhanced recurrence')
    >>> plt.tight_layout()
    >>> plt.show()
    '''

    if min_ratio is None:
        min_ratio = 1./max_ratio
    elif min_ratio > max_ratio:
        raise ParameterError('min_ratio={} cannot exceed max_ratio={}'.format(min_ratio, max_ratio))

    R_smooth = None
    for ratio in np.logspace(np.log2(min_ratio), np.log2(max_ratio), num=n_filters, base=2):
        kernel = diagonal_filter(window, n, slope=ratio, zero_mean=zero_mean)

        if R_smooth is None:
            R_smooth = scipy.ndimage.convolve(R, kernel, **kwargs)
        else:
            # Compute the point-wise maximum in-place
            np.maximum(R_smooth, scipy.ndimage.convolve(R, kernel, **kwargs),
                       out=R_smooth)

    if clip:
        # Clip the output in-place
        np.clip(R_smooth, 0, None, out=R_smooth)

    return R_smooth
