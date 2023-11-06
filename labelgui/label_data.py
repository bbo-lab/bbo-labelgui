from pathlib import Path
import numpy as np

version = 0.4


def update(labels, labeler="_unknown"):
    assert labels["version"] <= version, "Please update ACM traingui"

    # Before versioning
    if "version" not in labels or labels["version"] <= 0.2:
        if "labeler" not in labels:
            labels["labeler"] = {}
        if "labeler_list" not in labels:
            labels["labeler_list"] = [labeler]
        if labeler not in labels["labeler_list"]:
            labels["labeler_list"].append(labeler)

        labeled_frame_idxs = get_labeled_frame_idxs(labels)
        labeler_idx = labels["labeler_list"].index(labeler)

        for f_idx in labeled_frame_idxs:
            if f_idx not in labels["labeler"]:
                labels["labeler"][f_idx] = labeler_idx
            if f_idx not in labels["fr_times"]:
                labels["fr_times"][f_idx] = 0

    data_shape = get_data_shape(labels)

    if labels["version"] <= 0.3:
        print("Updating")
        labeler = {}
        point_times = {}
        for ln in labels["labels"]:
            labeler[ln] = {}
            point_times[ln] = {}
            for fr_idx in labels['labels'][ln]:
                labeler[ln][fr_idx] = np.ones(data_shape[0], dtype=np.uint16) * labels['labeler'][fr_idx]
                nanmask = np.any(np.isnan(labels["labels"][ln][fr_idx]), axis=1)
                labeler[ln][fr_idx][nanmask] = 0
                point_times[ln][fr_idx] = np.ones(data_shape[0], dtype=np.uint64) * labels['fr_times'][fr_idx]

        labels["point_times"] = point_times
        labels["labeler"] = labeler
        labels.pop("fr_times")

    # Bring labeler list in shape (add specials etc)
    make_global_labeler_list([labels])

    labels["version"] = version
    return labels


def load(file_path):
    if isinstance(file_path, str):
        file_path = Path(file_path)
    print("Loading", file_path.as_posix())
    labels = np.load(file_path, allow_pickle=True)["arr_0"][()]

    labels = update(labels, labeler=file_path.parent.parent.stem)

    return labels


def get_labels(labels):
    return list(labels["labels"].keys())


def get_labeled_frame_idxs(labels):
    frames = set()
    for ln in labels["labels"]:
        frames.update(set(labels["labels"][ln].keys()))

    return sorted(list(frames))

def get_frame_labelers(labels, fr_idx, cam_idx=None):
    labeler_idxs = set()
    for ln in labels["labeler"]:
        if fr_idx in labels["labeler"][ln]:
            if cam_idx is None:
                labeler_idxs.update(labels["labeler"][ln][fr_idx])
            else:
                labeler_idxs.update(labels["labeler"][ln][fr_idx][(cam_idx,),])


    labelers = [labels["labeler_list"][i] for i in labeler_idxs]
    if "_unmarked" in labelers:
        labelers.pop(labelers.index("_unmarked"))
    return labelers


def merge(labels_list: list, target_file=None, overwrite=False):
    # Load data from files
    labels_files = None
    if isinstance(labels_list[0], str):
        labels_files = [Path(lf).expanduser().resolve() for lf in labels_list]
    elif isinstance(labels_list[0], Path):
        labels_files = labels_list
    else:
        assert target_file is not None, "target_file is only supported if labels_list contains paths"

    # Normalize path of target_file
    if isinstance(target_file, str):
        target_file = Path(target_file).expanduser().resolve()

    # Add target files as first labels file if existing
    if target_file is not None and target_file.is_file():
        labels_files.insert(0, target_file)

    # Load data from labels files
    if labels_files is not None:
        labels_list = [load(lf.as_posix()) for lf in labels_files]

    make_global_labeler_list(labels_list)

    # Merge file-wise
    target_labels = labels_list[0]
    data_shape = get_data_shape(target_labels)

    for labels in labels_list[1:]:
        initialize_target(labels, target_labels, data_shape)

        for ln in labels["labels"]:
            for fr_idx in labels["labels"][ln]:
                target_cam_mask = target_labels["labels"][ln][fr_idx] != 0
                source_cam_mask = labels["labels"][ln][fr_idx] != 0
                source_newer_mask = target_labels["point_times"][ln][fr_idx] < labels["point_times"][ln][fr_idx]

                replace_mask = source_cam_mask & source_newer_mask

                if not overwrite:
                    replace_mask &= (~target_cam_mask)

                target_labels["labels"][ln][fr_idx][replace_mask] = labels["labels"][ln][fr_idx][replace_mask]
                target_labels["labeler"][ln][fr_idx][replace_mask] = labels["labeler"][ln][fr_idx][replace_mask]
                target_labels["point_times"][ln][fr_idx][replace_mask] = labels["point_times"][ln][fr_idx][replace_mask]

    sort_dictionaries(target_labels)

    if target_file is not None:
        np.savez(target_file, target_labels)
    return target_labels


def combine_cams(labels_list: list, target_file=None):
    # Normalize path of target_file
    if isinstance(target_file, str):
        target_file = Path(target_file).expanduser().resolve()

    # Load data from files
    for i_l, label in enumerate(labels_list):
        if isinstance(label, str):
            if label == "None":
                labels_list[i_l] = None
                continue
            labels_list[i_l] = Path(label)
        if isinstance(label, Path):
            labels_list[i_l] = labels_list[i_l].expanduser().resolve()
        labels_list[i_l] = load(labels_list[i_l].as_posix())

    make_global_labeler_list(labels_list)

    target_labels = get_empty_labels()
    target_labels['labeler_list'] = make_global_labeler_list(labels_list)

    data_shape = (len(labels_list), 2)

    for cam_idx, labels in enumerate(labels_list):
        if labels is None:
            continue

        # Walk through frames
        initialize_target(labels, target_labels, data_shape)

        for ln in labels["labels"]:
            for fr_idx in labels["labels"][ln]:
                target_labels["labels"][ln][fr_idx][cam_idx] = labels["labels"][ln][fr_idx]
                target_labels["point_times"][ln][fr_idx][cam_idx] = labels["point_times"][ln][fr_idx]
                target_labels["labeler"][ln][fr_idx][cam_idx] = labels["labeler"][ln][fr_idx]

    sort_dictionaries(target_labels)

    if target_file is not None:
        np.savez(target_file, target_labels)
    return target_labels


def initialize_target(labels, target_labels, data_shape):
    # Walk through frames
    for ln in labels["labels"]:
        # Initialize label key
        if ln not in target_labels["labels"]:
            target_labels["labels"][ln] = {}
        if ln not in target_labels["labeler"]:
            target_labels["labeler"][ln] = {}
        if ln not in target_labels["point_times"]:
            target_labels["point_times"][ln] = {}
        for fr_idx in labels["labels"][ln]:
            # Initialize frame index
            if fr_idx not in target_labels["labels"][ln]:
                target_labels["labels"][ln][fr_idx] = \
                    np.full(data_shape, np.nan)
            if fr_idx not in target_labels["labeler"][ln]:
                target_labels["labeler"][ln][fr_idx] = \
                    np.full(data_shape[0], 0, dtype=np.uint16)
            if fr_idx not in target_labels["point_times"][ln]:
                target_labels["point_times"][ln][fr_idx] = \
                    np.full(data_shape[0], 0, dtype=np.uint64)


def sort_dictionaries(target_labels):
    # Sort dictionaries
    for label in target_labels["labeler"]:
        target_labels["labeler"][label] = dict(sorted(target_labels["labeler"][label].items()))
    target_labels["labeler"] = dict(sorted(target_labels["labeler"].items()))

    for label in target_labels["point_times"]:
        target_labels["point_times"][label] = dict(sorted(target_labels["point_times"][label].items()))
    target_labels["point_times"] = dict(sorted(target_labels["point_times"].items()))

    for label in target_labels["labels"]:
        target_labels["labels"][label] = dict(sorted(target_labels["labels"][label].items()))
    target_labels["labels"] = dict(sorted(target_labels["labels"].items()))


def make_global_labeler_list(labels_list):
    # This changes labeler_list in place!!!
    # Create a new global list of all labelers
    labeler_list_all = []
    for labels in labels_list:
        if "labeler_list" in labels:
            labeler_list_all += labels["labeler_list"]
    labeler_list_all += ["_unknown"]
    labeler_list_all += ["_unmarked"]

    # Make unique and sorted
    labeler_list_all = sorted(list(set(labeler_list_all)))
    # Get specials to the front
    labeler_list_all.pop(labeler_list_all.index("_unmarked"))
    labeler_list_all.pop(labeler_list_all.index("_unknown"))
    labeler_list_all.insert(0, "_unknown")
    labeler_list_all.insert(0, "_unmarked")
    print(labeler_list_all)

    # Rewrite to global index list
    for labels in labels_list:
        for ln in labels["labels"]:
            for fr_idx in labels['labels'][ln]:
                for i, labeler_idx in enumerate(labels['labeler'][ln][fr_idx]):
                    labeler = labels["labeler_list"][labeler_idx]
                    labels['labeler'][ln][fr_idx][i] = labeler_list_all.index(labeler)
        labels["labeler_list"] = labeler_list_all
    return labeler_list_all


def get_data_shape(labels):
    shape = None
    for ln in labels['labels']:
        if shape is not None:
            break
        for fr_idx in labels['labels'][ln]:
            shape = labels['labels'][ln][fr_idx].shape
    return shape


def get_empty_labels():
    return {
        'labels': {},
        'point_times': {},
        'labeler_list': [],
        'labeler': {},
        'version': version,
    }
