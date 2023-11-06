import os
import re
from pathlib import Path
import shutil

def load_cfg(path):
    cfg_file = open(path, 'r')
    configtxt = cfg_file.read()
    datadir = datadir = re.sub(r'\\', '/', os.path.dirname(os.path.abspath(path)))+'/../../../data'
    cfg = eval(configtxt) # this is ugly since eval is used (make sure only trusted strings are evaluated)
    cfg_file.close()
    #sys.path.insert(0,os.path.dirname(path))
    #from labeling_gui_cfg import cfg
    #sys.path.remove(os.path.dirname(path))
    return cfg


def save_cfg(path: Path, cfg):
    with open(path, 'w') as file_cfg:
        file_cfg.write('{\n')
        for key in cfg.keys():
            if isinstance(cfg[key], str):
                line = f"  '{key}': '{cfg[key]}',\n"
            else:
                line = f"  '{key}': {cfg[key]},\n"
            file_cfg.write(line)
        file_cfg.write('}\n')
    return cfg


def archive_cfg(cfg, target_dir: Path):
    if isinstance(cfg, Path):
        shutil.copy(cfg, target_dir.as_posix())
        cfg = load_cfg(cfg)

    save_cfg(target_dir / "labeling_gui_cfg_processed.py", cfg)