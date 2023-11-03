import argparse
import os
from pathlib import Path
from labelgui import label_data
from labelgui import check


def main():
    # Parse inputs
    parser = argparse.ArgumentParser(description="ACM-traingui - Simple GUI to .")
    parser.add_argument('INPUT_PATH', type=str, help="Directory with detect job configuration")
    parser.add_argument('--labels', type=str, required=False, nargs='*', default=None,
                        help="If given, merges labes.npz in given dirs into labels.npz file specified in INPUT_PATH "
                             "config file")
    parser.add_argument('--merge', type=str, required=False, nargs='*', default=None,
                        help="If given, merges given labes.npz into labels.npz file specified in INPUT_PATH")
    parser.add_argument('--add', type=str, required=False, nargs='*', default=None,
                        help="Like merge, but never overwrites target data")
    parser.add_argument('--combine_cams', type=str, required=False, nargs='*', default=None,
                        help="If given, merges given labes.npz into a labels.npz file specified in INPUT_PATH, "
                             "where each labels file stands for a separate camera. 'None' serves as a placeholder.")
    parser.add_argument('--strict', required=False, action="store_true",
                        help="With --labels, merges only frames where frames were labeled in all cameras")
    parser.add_argument('--check', type=str, required=False, nargs='?', default=None, const='-',
                        help="Prints sorted list of square errors for labels in INPUT_PATH/labels.npz. Supply either "
                             "calibration file, a path to a labeling_gui_cfg.py or '-'/nothing to load "
                             "labeling_gui_cfg.py in directory of labels.npy")
    parser.add_argument('--master', required=False, action="store_true",
                        help="Switches between master mode and worker mode")

    args = parser.parse_args()
    input_path = os.path.expanduser(args.INPUT_PATH)
    print(input_path)

    if args.merge is not None:
        label_data.merge(args.merge, target_file=input_path, overwrite=True)
    if args.add is not None:
        label_data.merge(args.add, target_file=input_path, overwrite=False)
    elif args.combine_cams is not None:
        label_data.combine_cams(args.combine_cams, target_file=input_path)
    elif args.check is not None:
        check.check_triangulation_error(args.check, input_path)
    elif args.master:
        from labelgui import labeling_gui
        labeling_gui.main(Path('.'), config_file=input_path, master=True)
    else:
        from labelgui import labeling_gui
        input_path = Path(input_path)
        if input_path.name == "analysis":
            input_path /= "pose"
        labeling_gui.main(input_path, master=False)
    return


if __name__ == '__main__':
    main()
