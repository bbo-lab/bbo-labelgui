#!/usr/bin/env python3
import hashlib

import numpy as np
import os
import sys
import calibcamlib
import time
import typing
from typing import List, Dict, Optional

from PyQt5 import QtCore
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIntValidator, QCursor
from PyQt5.QtWidgets import QAbstractItemView, \
    QApplication, \
    QFrame, \
    QFileDialog, \
    QGridLayout, \
    QLabel, \
    QLineEdit, \
    QListWidget, \
    QMainWindow, \
    QPushButton, QComboBox

from matplotlib import colors as mpl_colors
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT
from matplotlib.image import AxesImage
from matplotlib.figure import Figure

from pathlib import Path

from bbo import label_lib, path_management as bbo_pm
from labelgui.config import load_cfg, archive_cfg
from labelgui.helper_gui import update_button_stylesheet, disable_button, get_button_status, toggle_button
from labelgui.select_user import SelectUserWindow
from labelgui.helper_video import read_video_meta
import svidreader

import paho.mqtt.client as mqtt


class MainWindow(QMainWindow):
    def __init__(self, drive: Path, file_config=None, master=True, parent=None, sync:str|bool=False):
        super(MainWindow, self).__init__(parent)

        # Parameters
        self.drive = drive
        self.master = master

        self.mqtt_client = None

        if self.master:
            if file_config is None:
                file_config = 'labeling_gui_cfg.py'  # use hard coded path here
            self.cfg = load_cfg(file_config)
        else:
            if os.path.isdir(self.drive):
                self.user, job, correct_exit = SelectUserWindow.start(drive)
                if correct_exit:
                    if job is not None:
                        file_config = self.drive / 'data' / 'user' / self.user / 'jobs' / f'{job}.py'
                    else:
                        file_config = self.drive / 'data' / 'user' / self.user / 'labeling_gui_cfg.py'
                    self.cfg = load_cfg(file_config)
                else:
                    sys.exit()
            else:
                print('ERROR: Server is not mounted')
                sys.exit()
        print("++++++++++++++++++++++++++++++++++++")
        print("file_config: ",  file_config)
        print("++++++++++++++++++++++++++++++++++++")
        self.file_config = file_config
        self.model = None

        # Files
        self.standardCalibrationFile = Path(self.cfg['standardCalibrationFile'])
        self.standardOriginCoordFile = Path(self.cfg['standardOriginCoordFile'])
        self.standardModelFile = Path(self.cfg['standardModelFile'])
        self.standardLabelsFile = Path(self.cfg['standardLabelsFile'])
        self.standardSketchFile = Path(self.cfg['standardSketchFile'])
        self.standardLabelsFolder = None

        # Data load status
        self.recordingIsLoaded = False
        self.calibrationIsLoaded = False
        self.originIsLoaded = False
        self.modelIsLoaded = False
        self.labelsAreLoaded = False
        self.sketchIsLoaded = False

        # Loaded data
        self.cameras: List[Dict] = []
        self.calibration = None
        self.camera_system = None
        self.origin_coord = None
        self.sketch = None

        # Controls
        self.controls = {
            'canvases': {},
            'toolbars': {},
            'figs': {},
            'axes': {},
            'plots': {},
            'frames': {},
            'grids': {},
            'lists': {},
            'fields': {},
            'labels': {},
            'buttons': {},
            'texts': {},
        }

        self.dataset_name = Path(self.cfg['standardRecordingFolder']).name
        self.labels = label_lib.get_empty_labels()
        self.ref_labels = label_lib.get_empty_labels()

        if "standardReferenceLabelFile" not in self.cfg:
            self.cfg['standardReferenceLabelFile'] = True
        if isinstance(self.cfg['standardReferenceLabelFile'], bool) and self.cfg['standardReferenceLabelFile']:
            self.cfg['standardReferenceLabelFile'] = bbo_pm.decode_path(f"{{storage}}/analysis/pose/data/references/{self.dataset_name}.yml")
        if isinstance(self.cfg['standardReferenceLabelFile'], str):
            ref_labels_file = Path(self.cfg['standardReferenceLabelFile'])
            if ref_labels_file.is_file():
                self.ref_labels = label_lib.load(ref_labels_file)
            else:
                print(f"ref_labels_file {ref_labels_file.as_posix()} not found")

        self.neighbor_points = {}

        # Sketch zoom stuff
        self.sketch_zoom_dy = None
        self.sketch_zoom_dx = None
        if 'sketchZoomScale' in self.cfg:
            self.sketch_zoom_scale = self.cfg['sketchZoomScale']
        else:
            self.sketch_zoom_scale = 0.1

        self.dx = int(128)
        self.dy = int(128)
        self.vmin = int(0)
        self.vmax = int(127)
        self.dFrame = self.cfg['dFrame']

        self.minPose = int(self.cfg['minPose'])
        self.maxPose = int(self.cfg['maxPose'])

        self.allowed_poses = np.arange(self.minPose, self.maxPose, self.dFrame, dtype=int)

        if 'allowed_cams' in self.cfg:
            self.allowed_cams = [int(i) for i in self.cfg['allowed_cams']]
        else:
            self.allowed_cams = list(range(len(self.cfg['standardRecordingFileNames'])))
        self.allowed_cams = sorted(self.allowed_cams)

        self.pose_idx = self.minPose

        if 'cam' in self.cfg:
            self.camera_idx = self.cfg['cam']
        else:
            self.camera_idx = min(self.allowed_cams)

        self.colors = []
        self.init_colors()

        self.autoSaveCounter = int(0)

        self.setGeometry(0, 0, 1024, 768)
        self.showMaximized()

        self.init_files_folders()
        self.set_controls()
        self.set_layout()

        self.plot2d_ini()

        self.setFocus()
        self.setWindowTitle('Labeling GUI')
        self.show()

        self.sketch_init()

        self.sync = sync
        self.mqtt_connect()

    def mqtt_connect(self):
        if isinstance(self.sync,bool):
            return
        try:
            self.mqtt_client = mqtt.Client()
            self.mqtt_client.on_message = self.mqtt_on_message
            self.mqtt_client.connect("127.0.0.1", 1883, 60)
            self.mqtt_client.subscribe(self.sync)
            self.mqtt_client.loop_start()
        except ConnectionRefusedError:
            print("ERROR: No connection to MQTT server.")
            self.mqtt_client = None

    def mqtt_publish(self):
        if self.mqtt_client is not None:
            try:
                self.mqtt_client.publish(self.sync, payload=str(self.pose_idx))
            except ConnectionRefusedError:
                print("ERROR: No connection to MQTT server.")
                self.mqtt_client = None

    def mqtt_on_message(self, client, userdata, message):
        print(f"Received message '{message.payload.decode()}' on topic '{message.topic}'")
        match message.topic:
            case "bbo/sync/fr_idx":
                fr_idx = int(message.payload.decode())
                self.set_pose_idx(fr_idx, mqtt_publish=False)

    def init_files_folders(self):
        standard_recording_folder = Path(self.cfg['standardRecordingFolder'])

        # create folder structure / save backup / load last pose
        if not self.master:
            self.init_assistant_folders(standard_recording_folder)
            self.init_autosave()
            archive_cfg(self.file_config, self.standardLabelsFolder )
            self.restore_last_pose_idx()

        if self.cfg['autoLoad']:
            rec_file_names = (
                [bbo_pm.decode_path(standard_recording_folder / i).expanduser().resolve() for i in self.cfg['standardRecordingFileNames']]
            )
            self.load_recordings_from_names(rec_file_names)

            self.load_calibrations()
            self.load_origin()
            # self.load_model()
            self.load_sketch()
            self.load_labels()

    def load_labels(self, labels_file: typing.Optional[Path] = None):
        if labels_file is None:
            self.standardLabelsFile = self.standardLabelsFolder / 'labels.yml'
        else:
            self.standardLabelsFile = labels_file

        print("labels_file: ", self.standardLabelsFile)
        try:
            self.labels = label_lib.load(self.standardLabelsFile)
            self.labelsAreLoaded = True
        except FileNotFoundError as e:
            print(f'WARNING: Autoloading failed. Labels file {self.standardLabelsFile} does not exist.')

    def load_sketch(self, sketch_file: typing.Optional[Path] = None):
        if sketch_file is None:
            sketch_file = self.standardSketchFile

        # load sketch
        if sketch_file.is_file():
            self.sketch = np.load(sketch_file.as_posix(), allow_pickle=True)[()]

            sketch = self.get_sketch()
            self.sketch_zoom_dx = np.max(np.shape(sketch)) * self.sketch_zoom_scale
            self.sketch_zoom_dy = np.max(np.shape(sketch)) * self.sketch_zoom_scale

            self.sketchIsLoaded = True
        else:
            print(f'WARNING: Autoloading failed. Sketch file {self.standardSketchFile} does not exist.')

        for label_name in self.get_sketch_labels():
            if label_name not in self.labels['labels']:
                self.labels['labels'][label_name] = {}

    def get_sketch(self):
        return self.sketch['sketch'].astype(np.uint8)

    def get_sketch_labels(self):
        return self.sketch['sketch_label_locations']

    def get_sketch_label_coordinates(self):
        return np.array(list(self.get_sketch_labels().values()), dtype=np.float64)

    def load_origin(self, origin_file: typing.Optional[Path] = None):
        if origin_file is None:
            origin_file = self.standardOriginCoordFile

        if origin_file.is_file():
            self.originIsLoaded = True
            self.origin_coord = np.load(origin_file.as_posix(), allow_pickle=True)[()]
        else:
            print(f'WARNING: Autoloading failed. Origin/Coord file {origin_file} does not exist.')

    def get_origin_coord(self):
        return self.origin_coord['origin'], self.origin_coord['coord']

    def load_calibrations(self, calibrations_file: typing.Optional[Path] = None):
        if calibrations_file is None:
            calibrations_file = self.standardCalibrationFile

        if calibrations_file.is_file():
            self.camera_system = calibcamlib.Camerasystem.from_calibcam_file(calibrations_file.as_posix())
            self.calibrationIsLoaded = True

            self.calibration = np.load(calibrations_file.as_posix(), allow_pickle=True)[()]
        else:
            print(f'WARNING: Autoloading failed. Calibration file {calibrations_file} does not exist.')

    # noinspection PyPep8Naming
    def get_calibration_params(self):
        A_val = self.calibration['A_fit']
        A = np.zeros((len(self.cameras), 3, 3), dtype=np.float64)
        for i in range(len(self.cameras)):
            A[i][0, 0] = A_val[i, 0]
            A[i][0, 2] = A_val[i, 1]
            A[i][1, 1] = A_val[i, 2]
            A[i][1, 2] = A_val[i, 3]
            A[i][2, 2] = 1.0

        return {
            'A': A,
            'k': self.calibration['k_fit'],
            'rX1': self.calibration['rX1_fit'],
            'RX1': self.calibration['RX1_fit'],
            'tX1': self.calibration['tX1_fit'],
        }

    def load_recordings_from_names(self, names):
        # load recording
        # try:
        self.recordingIsLoaded = True

        cameras = []
        for file_name in names:
            if file_name:
                print(f"File name: {file_name}")
                print(svidreader.__file__)
                reader = svidreader.get_reader(file_name.as_posix(), backend="iio", cache=True)
                print(1)
                header = read_video_meta(reader)
                print(2)
                cam = {
                    'file_name': file_name,
                    'reader': reader,
                    'header': header,
                    'x_lim_prev': (0, header['sensorsize'][0]),
                    'y_lim_prev': (0, header['sensorsize'][1]),
                    'rotate': False,
                }
                cameras.append(cam)
            else:
                print(f'WARNING: Invalid recording file {file_name}')

        self.cameras = cameras
        # except Exception as e:

        # print(f'WARNING: Autoloading failed. Recording files do not exist: {file_name} {e}')

    def get_n_poses(self):
        return [cam["header"]["nFrames"] for cam in self.cameras]

    def get_sensor_sizes(self):
        return [cam["header"]["sensorsize"] for cam in self.cameras]

    def get_x_res(self):
        return [ss[0] for ss in self.get_sensor_sizes()]

    def get_y_res(self):
        return [ss[1] for ss in self.get_sensor_sizes()]

    def restore_last_pose_idx(self):
        # last pose
        file_exit_status = self.standardLabelsFolder / 'exit_status.npy'
        if file_exit_status.is_file():
            exit_status = np.load(file_exit_status.as_posix(), allow_pickle=True)[()]
            self.set_pose_idx(exit_status['i_pose'])

    def init_autosave(self):
        # autosave
        autosavefolder = self.standardLabelsFolder / 'autosave'
        if not autosavefolder.is_dir():
            os.makedirs(autosavefolder)
        archive_cfg(self.file_config, autosavefolder)

    def init_assistant_folders(self, standard_recording_folder: Path):
        # folder structure
        userfolder = self.drive / 'user' / self.user
        if not userfolder.is_dir():
            os.makedirs(userfolder)
        resultsfolder = userfolder / standard_recording_folder.name
        if not resultsfolder.is_dir():
            os.makedirs(resultsfolder)
        self.standardLabelsFolder = resultsfolder.expanduser().resolve()
        # backup
        backupfolder = self.standardLabelsFolder / 'backup'
        if not backupfolder.is_dir():
            os.mkdir(backupfolder)
        file = self.standardLabelsFolder / 'labeling_gui_cfg.py'
        if file.is_file():
            archive_cfg(file, backupfolder)
        file = self.standardLabelsFolder / 'labels.yml'
        try:
            labels_old = label_lib.load(file)
            label_lib.save(backupfolder / 'labels.yml', labels_old)
        except FileNotFoundError as e:
            pass

    def init_colors(self):
        colors = dict(mpl_colors.BASE_COLORS, **mpl_colors.CSS4_COLORS)
        # Sort colors by hue, saturation, value and name.
        by_hsv = sorted((tuple(mpl_colors.rgb_to_hsv(mpl_colors.to_rgba(color)[:3])), name)
                        for name, color in colors.items())
        sorted_names = [name for hsv, name in by_hsv]
        for i in range(24, -1, -1):
            self.colors = self.colors + sorted_names[i::24]

    def get_pose_idx(self):
        return self.pose_idx

    def set_pose_idx(self, pose_idx, mqtt_publish=True):
        pose_idx = int(pose_idx)

        pose_idx = self.allowed_poses[np.argmin(np.abs(self.allowed_poses - pose_idx))]

        self.pose_idx = pose_idx
        self.plot2d_change_frame()
        if 'next' in self.controls['buttons']:
            self.controls['buttons']['next'].clearFocus()

        if mqtt_publish:
            self.mqtt_publish()

    def combo_lists_cams_change(self, new_index):
        print(new_index, type(new_index))
        self.set_camera_idx(int(self.controls['lists']['cams'].itemText(new_index).split(" ")[1]))

    def set_camera_idx(self, camera_idx):

        if not isinstance(camera_idx, int):
            camera_idx = int(camera_idx)

        if camera_idx not in self.allowed_cams:
            return

        self.camera_idx = camera_idx
        self.plot2d_change_frame()
        self.controls['buttons']['next'].clearFocus()

    def set_layout(self):
        # frame main
        frame_main = QFrame()
        self.setStyleSheet("background-color: black;")
        layout_grid = QGridLayout()
        layout_grid.setSpacing(10)
        frame_main.setMinimumSize(512, 512)
        # frame for 2d views
        self.controls['frames']['views2d'].setStyleSheet("background-color: white;")
        layout_grid.setRowStretch(0, 2)
        layout_grid.setColumnStretch(0, 2)
        layout_grid.addWidget(self.controls['frames']['views2d'], 0, 0, 2, 4)

        self.controls['grids']['views2d'].setSpacing(0)
        self.controls['frames']['views2d'].setLayout(self.controls['grids']['views2d'])

        # frame for 3d model
        self.controls['frames']['views3d'].setStyleSheet("background-color:  white;")
        layout_grid.setRowStretch(0, 1)
        layout_grid.setColumnStretch(4, 1)
        layout_grid.addWidget(self.controls['frames']['views3d'], 0, 4)

        self.controls['grids']['views3d'].setSpacing(0)
        self.controls['frames']['views3d'].setLayout(self.controls['grids']['views3d'])

        # frame for controls
        self.controls['frames']['controls'].setStyleSheet("background-color: white;")
        layout_grid.setRowStretch(1, 1)
        layout_grid.setColumnStretch(4, 1)
        layout_grid.addWidget(self.controls['frames']['controls'], 1, 4)
        # add to grid
        frame_main.setLayout(layout_grid)
        self.setCentralWidget(frame_main)

    def get_current_label(self):
        current_label_item = self.controls['lists']['labels'].currentItem()
        if current_label_item is not None:
            current_label_name = current_label_item.text()
        else:
            current_label_name = self.controls['lists']['labels'].item(0).text()
        return current_label_name

    def plot2d_ini(self):
        for i in reversed(range(self.controls['grids']['views2d'].count())):
            widget_to_remove = self.controls['grids']['views2d'].itemAt(i).widget()
            self.controls['grids']['views2d'].removeWidget(widget_to_remove)
            widget_to_remove.setParent(None)

        fig = Figure(tight_layout=True)
        fig.clear()
        self.controls['figs']['2d'] = fig
        canvas = FigureCanvasQTAgg(fig)
        canvas.setParent(self.controls['frames']['views2d'])
        ax = fig.add_subplot(111)
        ax.clear()
        self.controls['axes']['2d'] = ax
        self.plot2d_plot(self.controls['axes']['2d'], self.camera_idx)

        self.controls['grids']['views2d'].addWidget(canvas, 0, 0)
        self.controls['toolbars']['views2d'] = NavigationToolbar2QT(canvas, self)
        release_zoom_callback = self.controls['toolbars']['views2d'].release_zoom

        def rzc(*args, **kwargs):
            release_zoom_callback(*args, **kwargs)
            self.disable_zoom()

        self.controls['toolbars']['views2d'].release_zoom = rzc
        self.controls['toolbars']['views2d'].hide()

        self.controls['frames']['views2d'].setCursor(QCursor(QtCore.Qt.CrossCursor))

        if get_button_status(self.controls['buttons']['zoom']):
            self.button_zoom_press()
        if get_button_status(self.controls['buttons']['pan']):
            self.button_pan_press()

        fig.canvas.mpl_connect('button_press_event',
                               lambda event: self.plot2d_click(event))

    def plot2d_draw(self):
        if get_button_status(self.controls['buttons']['zoom']):
            self.button_zoom_press()
        if get_button_status(self.controls['buttons']['pan']):
            self.button_pan_press()

        self.plot2d_plot(self.controls['axes']['2d'], self.camera_idx)
        self.controls['figs']['2d'].canvas.draw()

    def plot2d_plot(self, ax, i_cam):
        self.plot2d_update_image(ax, i_cam)
        self.plot2d_draw_labels(ax)

    def plot2d_update_image(self, ax, i_cam):
        reader = self.cameras[i_cam]["reader"]
        img = reader.get_data(self.pose_idx)
        if self.controls['plots']['image2d'] is None:
            self.controls['plots']['image2d'] = ax.imshow(img,
                                                          aspect=1,
                                                          cmap='gray',
                                                          vmin=self.vmin,
                                                          vmax=self.vmax)
#            print(f"img {self.pose_idx}: {hashlib.md5(img).hexdigest()}")
            ax.legend('',
                      facecolor=self.colors[i_cam % np.size(self.colors)],
                      loc='upper left',
                      bbox_to_anchor=(0, 1))
            ax.axis('off')
        else:
            self.controls['plots']['image2d'].set_array(img)
            self.controls['plots']['image2d'].set_clim(self.vmin, self.vmax)
        x_res = self.get_x_res()
        y_res = self.get_y_res()
        ax.set_xlim(0.0, x_res[i_cam] - 1)
        ax.set_ylim(0.0, y_res[i_cam] - 1)
        if self.cfg['invert_xaxis']:
            ax.invert_xaxis()
        if self.cfg['invert_yaxis']:
            ax.invert_yaxis()
        if self.cameras[i_cam]["rotate"]:
            ax.invert_xaxis()
            ax.invert_yaxis()

    def plot2d_update(self):
        self.plot2d_draw_labels(self.controls['axes']['2d'])
        self.controls['figs']['2d'].canvas.draw()

    def plot2d_draw_labels(self, ax):
        # ax.lines = list()

        cam_idx = self.camera_idx
        frame_idx = self.get_pose_idx()
        current_label_name = self.get_current_label()

        # Initialize
        if '2d' not in self.controls['plots']:
            self.controls['plots']['2d'] = {}
        if '2d_neighbor' not in self.controls['plots']:
            self.controls['plots']['2d_neighbor'] = {}
        if '2d_ref' not in self.controls['plots']:
            self.controls['plots']['2d_ref'] = {}
        if '2d_line' not in self.controls['plots']:
            self.controls['plots']['2d_line'] = {}

        # Remove labels
        for tn in ["2d", "2d_neighbor", "2d_ref", "2d_line"]:
            for label_name in self.controls['plots'][tn]:
                try:
                    self.controls['plots'][tn][label_name].remove()
                except ValueError:
                    pass

        # Build neighbor points
        # TODO: Do a better estimation, e.g. adjust to close by points
        for label_name in self.labels['labels']:
            self.neighbor_points[label_name] = np.full((1, 2), np.nan)
            for offs in range(1, 4):
                # Try to take mean of symmetrical situation
                if frame_idx - offs in self.labels['labels'][label_name] and \
                        not np.any(np.isnan(frame_idx - offs in self.labels['labels'][label_name])) and \
                        frame_idx + offs in self.labels['labels'][label_name] and \
                        not np.any(np.isnan(frame_idx + offs in self.labels['labels'][label_name])):
                    self.neighbor_points[label_name] = np.nanmean([self.neighbor_points[label_name],
                                                                   self.labels['labels'][label_name][frame_idx - offs][(self.camera_idx,),],
                                                                   self.labels['labels'][label_name][frame_idx + offs][(self.camera_idx,),]
                                                                   ],
                                                                  axis=0)
                    break

            if np.any(np.isnan(self.neighbor_points[label_name])):
                # Fill from one closest neighbor, TODO: Counter from other side even if not symmetrical?
                for offs in [-1, 1, -2, 2, -3, 3]:
                    if frame_idx + offs in self.labels['labels'][label_name]:
                        self.neighbor_points[label_name] = self.labels['labels'][label_name][frame_idx + offs][(self.camera_idx,),]
                        break

        # Plot each label
        for label_name in self.labels['labels']:

            if frame_idx in self.labels['labels'][label_name] and \
                    not np.any(np.isnan(self.labels['labels'][label_name][frame_idx][cam_idx])):
                # Plot acutal labels
                point = self.labels['labels'][label_name][frame_idx][cam_idx, :]
                labeler = self.labels['labeler_list'][self.labels['labeler'][label_name][frame_idx][cam_idx]]

                if current_label_name == label_name:
                    plotparams = {
                        'color': 'darkgreen',
                        'markersize': 4,
                        'zorder': 3,
                    }
                else:
                    plotparams = {
                        'color': 'cyan',
                        'markersize': 3,
                        'zorder': 2,
                    }
                print(f"label {label_name} {frame_idx} {labeler}: {point}")
                self.controls['plots']['2d'][label_name] = ax.plot([point[0]], [point[1]],
                                                                   marker='o',
                                                                   **plotparams,
                                                                   )[0]
            else:
                # Plot neighbor points
                if current_label_name == label_name:
                    plotparams = {
                        'color': 'darkgreen',
                        'markersize': 4,
                        'zorder': 1,
                    }
                else:
                    plotparams = {
                        'color': 'cyan',
                        'markersize': 3,
                        'zorder': 0,
                    }
                self.controls['plots']['2d_neighbor'][label_name] = ax.plot(*self.neighbor_points[label_name][0],
                                                                            marker='+',
                                                                            **plotparams,
                                                                            )[0]

        for label_name in self.labels['labels']:
            if label_name in self.ref_labels['labels'] and frame_idx in self.ref_labels['labels'][label_name] and \
                    not np.any(np.isnan(self.ref_labels['labels'][label_name][frame_idx][cam_idx])):
                # Plot reference labels
                point = self.ref_labels['labels'][label_name][frame_idx][cam_idx, :]

                plotparams = {
                    'color': 'red',
                    'markersize': 3,
                    'zorder': 0,
                }

                self.controls['plots']['2d_ref'][label_name] = ax.plot([point[0]], [point[1]],
                                                                   marker='x',
                                                                   **plotparams,
                                                                   )[0]

                if frame_idx in self.labels['labels'][label_name] and \
                        not np.any(np.isnan(self.labels['labels'][label_name][frame_idx][cam_idx])):
                    line_coords = np.concatenate((self.labels['labels'][label_name][frame_idx][(cam_idx,), :],
                                                  self.ref_labels['labels'][label_name][frame_idx][(cam_idx,), :]), axis=0)
                    print("Drawing line", line_coords.shape, line_coords)
                    self.controls['plots']['2d_line'][label_name] = ax.plot(*line_coords.T,
                                                                            color="red",
                                                                            linewidth=1,
                                                                            zorder=0,
                                                                            )[0]

        print("=============")

    def plot2d_click(self, event):
        if not (get_button_status(self.controls['buttons']['zoom']) or
                get_button_status(self.controls['buttons']['pan'])):
            ax = event.inaxes

            # Initialize array
            fr_idx = self.get_pose_idx()
            label_name = self.get_current_label()
            cam_idx = self.camera_idx

            modifiers = QApplication.keyboardModifiers()

            if ax is not None:
                # Shift + Left mouse - select
                if event.button == 1 and modifiers == QtCore.Qt.ShiftModifier:
                    x = event.xdata
                    y = event.ydata
                    coords = np.array([x, y], dtype=np.float64)
                    point_dists = []
                    label_names = list(self.get_sketch_labels().keys())
                    for ln in label_names:
                        if ln in self.labels['labels'] and \
                                fr_idx in self.labels['labels'][ln] and \
                                len(self.labels['labels'][ln][fr_idx]) > cam_idx and \
                                not np.any(np.isnan(self.labels['labels'][ln][fr_idx][cam_idx])):
                            point_dists.append(
                                np.linalg.norm(self.labels['labels'][ln][fr_idx][cam_idx] - coords))
                        else:
                            point_dists.append(1000000000)

                    label_idx = point_dists.index(min(point_dists))
                    self.controls['lists']['labels'].setCurrentRow(label_idx)
                    self.list_labels_select()

                elif event.button == 1 and modifiers == QtCore.Qt.ControlModifier:
                    x = event.xdata
                    y = event.ydata
                    coords = np.array([x, y], dtype=np.float64)
                    point_dists = []
                    label_names = list(self.get_sketch_labels().keys())
                    for ln in label_names:
                        if ln in self.labels['labels'] and \
                                not np.any(np.isnan(self.neighbor_points[ln])):
                            point_dists.append(
                                np.linalg.norm(self.neighbor_points[ln] - coords))
                        else:
                            point_dists.append(1000000000)

                    label_idx = point_dists.index(min(point_dists))
                    self.controls['lists']['labels'].setCurrentRow(label_idx)
                    self.list_labels_select()

                # Left mouse - create
                elif event.button == 1:
                    x = event.xdata
                    y = event.ydata
                    if (x is not None) and (y is not None):
                        modifiers = QApplication.keyboardModifiers()
                        nextframe = self.get_pose_idx() + self.dFrame
                        if modifiers == QtCore.Qt.AltModifier:
                            self.autolabel(fr_idx, coords=[x,y], radius=5)
                        else:
                            self.add_label([x,y], label_name, cam_idx, fr_idx)

                        self.plot2d_update()
                        self.sketch_update()

                # Right mouse - delete
                elif event.button == 3:
                    if self.user not in self.labels["labeler_list"]:
                        self.labels["labeler_list"].append(self.user)

                    self.labels['labels'][label_name][fr_idx][cam_idx, :] = np.nan
                    # For synchronisation, deletion time and user must be recorded
                    self.labels['point_times'][label_name][fr_idx][cam_idx] = time.time()
                    self.labels['labeler'][label_name][fr_idx][cam_idx] = self.labels["labeler_list"].index(self.user)

                    self.plot2d_update()
                    self.sketch_update()

    def add_label(self, coords, label_name, cam_idx, fr_idx):
        data_shape = (len(self.cameras), 2)
        self.initialize_field(label_name, fr_idx, data_shape)
        if self.user not in self.labels["labeler_list"]:
            self.labels["labeler_list"].append(self.user)
        self.labels['labeler'][label_name][fr_idx][cam_idx] = self.labels["labeler_list"].index(
            self.user)
        self.labels['point_times'][label_name][fr_idx][cam_idx] = time.time()
        coords = np.array(coords, dtype=np.float64)
        self.labels['labels'][label_name][fr_idx][cam_idx] = coords


    def initialize_field(self, label_name, frame_idx, data_shape):
        if label_name not in self.labels['labels']:
            self.labels['labels'][label_name] = {}
        if label_name not in self.labels['point_times']:
            self.labels['point_times'][label_name] = {}
        if label_name not in self.labels['labeler']:
            self.labels['labeler'][label_name] = {}
        if frame_idx not in self.labels['labels'][label_name]:
            self.labels['labels'][label_name][frame_idx] = np.full(data_shape, np.nan, dtype=np.float64)
        if frame_idx not in self.labels['point_times'][label_name]:
            self.labels['point_times'][label_name][frame_idx] = np.full(data_shape[0], 0, dtype=np.uint64)
        if frame_idx not in self.labels['labeler'][label_name]:
            self.labels['labeler'][label_name][frame_idx] = np.full(data_shape[0], 0, dtype=np.uint16)

    # sketch
    def sketch_init(self):
        for i in reversed(range(self.controls['grids']['views3d'].count())):
            widget_to_remove = self.controls['grids']['views3d'].itemAt(i).widget()
            self.controls['grids']['views3d'].removeWidget(widget_to_remove)
            widget_to_remove.setParent(None)

        sketch = self.get_sketch()

        self.controls['figs']['sketch'].clear()

        # full
        ax_sketch_dims = [0 / 3, 1 / 18, 1 / 3, 16 / 18]
        self.controls['axes']['sketch'] = self.controls['figs']['sketch'].add_axes(ax_sketch_dims)
        self.controls['axes']['sketch'].clear()
        self.controls['axes']['sketch'].grid(False)
        self.controls['axes']['sketch'].imshow(sketch)
        self.controls['axes']['sketch'].axis('off')
        self.controls['axes']['sketch'].set_title('Full:',
                                                  ha='center', va='center',
                                                  zorder=0)

        self.controls['axes']['sketch'].set_xlim(
            [-self.sketch_zoom_dx / 2, np.shape(sketch)[1] + self.sketch_zoom_dx / 2])
        self.controls['axes']['sketch'].set_ylim(
            [-self.sketch_zoom_dy / 2, np.shape(sketch)[0] + self.sketch_zoom_dy / 2])
        self.controls['axes']['sketch'].invert_yaxis()
        # zoom
        ax_sketch_zoom_dims = [1 / 3, 5 / 18, 2 / 3, 12 / 18]
        self.controls['axes']['sketch_zoom'] = self.controls['figs']['sketch'].add_axes(ax_sketch_zoom_dims)
        self.controls['axes']['sketch_zoom'].imshow(sketch)
        self.controls['axes']['sketch_zoom'].set_xlabel('')
        self.controls['axes']['sketch_zoom'].set_ylabel('')
        self.controls['axes']['sketch_zoom'].set_xticks(list())
        self.controls['axes']['sketch_zoom'].set_yticks(list())
        self.controls['axes']['sketch_zoom'].set_xticklabels(list())
        self.controls['axes']['sketch_zoom'].set_yticklabels(list())
        self.controls['axes']['sketch_zoom'].set_title('Zoom:',
                                                       ha='center', va='center',
                                                       zorder=0)
        self.controls['axes']['sketch_zoom'].grid(False)

        self.controls['axes']['sketch_zoom'].set_xlim(
            [np.shape(sketch)[1] / 2 - self.sketch_zoom_dx, np.shape(sketch)[1] / 2 + self.sketch_zoom_dx])
        self.controls['axes']['sketch_zoom'].set_ylim(
            [np.shape(sketch)[0] / 2 - self.sketch_zoom_dy, np.shape(sketch)[0] / 2 + self.sketch_zoom_dy])
        self.controls['axes']['sketch_zoom'].invert_yaxis()
        # text
        self.controls['texts']['sketch'] = self.controls['figs']['sketch'].text(
            ax_sketch_dims[0] + ax_sketch_dims[2] / 2,
            ax_sketch_dims[1] / 2,
            'Label {:02d}:\n{:s}'.format(0, ''),
            ha='center', va='center',
            fontsize=18,
            zorder=2)

        self.controls['labels']['sketch'] = {}
        self.controls['labels']['sketch_zoom'] = {}

        self.sketch_init_labels()
        self.sketch_update()
        self.controls['grids']['views3d'].addWidget(self.controls['canvases']['sketch'])

        self.controls['canvases']['sketch'].mpl_connect('button_press_event',
                                                        lambda event: self.sketch_click(
                                                            event))

    def sketch_init_labels(self):
        dot_params = {
            'color': 'darkgreen',
            'marker': '.',
            'markersize': 2,
            'alpha': 1.0,
            'zorder': 2,
        }
        circle_params = {
            'color': 'darkgreen',
            'marker': 'o',
            'markersize': 40,
            'markeredgewidth': 4,
            'fillstyle': 'none',
            'alpha': 2 / 3,
            'zorder': 2,
        }

        for ln in ['sketch_dot', 'sketch_circle', 'sketch_zoom_dot', 'sketch_zoom_circle']:
            if ln in self.controls['plots']:
                self.controls['plots'][ln].remove()
        self.controls['plots']['sketch_dot'] = self.controls['axes']['sketch'].plot(
            [np.nan], [np.nan], **dot_params)[0]
        self.controls['plots']['sketch_circle'] = self.controls['axes']['sketch'].plot(
            [np.nan], [np.nan], **circle_params)[0]
        self.controls['plots']['sketch_zoom_dot'] = self.controls['axes']['sketch_zoom'].plot(
            [np.nan], [np.nan], **dot_params)[0]
        self.controls['plots']['sketch_zoom_circle'] = self.controls['axes']['sketch_zoom'].plot(
            [np.nan], [np.nan], **circle_params)[0]

        for label_name, label_location in self.get_sketch_labels().items():
            if label_name in self.controls['labels']['sketch']:
                self.controls['labels']['sketch'][label_name].remove()
            self.controls['labels']['sketch'][label_name] = \
                self.controls['axes']['sketch'].plot([label_location[0]], [label_location[1]],
                                                     marker='o',
                                                     color='orange',
                                                     markersize=3,
                                                     zorder=1)[0]

            if label_name in self.controls['labels']['sketch_zoom']:
                self.controls['labels']['sketch_zoom'][label_name].remove()
            self.controls['labels']['sketch_zoom'][label_name] = \
                self.controls['axes']['sketch_zoom'].plot([label_location[0]],
                                                          [label_location[1]],
                                                          marker='o',
                                                          color='orange',
                                                          markersize=5,
                                                          zorder=1)[0]

    def sketch_update(self):
        # Updates labels on the sketch
        sketch_labels = self.get_sketch_labels()
        label_names = list(self.labels['labels'].keys())

        for label_name in label_names:
            if label_name in self.controls['labels']['sketch']:
                self.controls['labels']['sketch'][label_name].set(color='orange')
                self.controls['labels']['sketch_zoom'][label_name].set(color='orange')

        current_label_name = self.get_current_label()
        if current_label_name is None or current_label_name not in sketch_labels:
            return

        (x, y) = sketch_labels[current_label_name].astype(np.float32)
        self.controls['plots']['sketch_dot'].set_data([x], [y])
        self.controls['plots']['sketch_circle'].set_data([x], [y])
        # zoom
        self.controls['plots']['sketch_zoom_dot'].set_data([x], [y])
        self.controls['plots']['sketch_zoom_circle'].set_data([x], [y])
        self.controls['axes']['sketch_zoom'].set_xlim([x - self.sketch_zoom_dx, x + self.sketch_zoom_dx])
        self.controls['axes']['sketch_zoom'].set_ylim([y - self.sketch_zoom_dy, y + self.sketch_zoom_dy])
        self.controls['axes']['sketch_zoom'].invert_yaxis()

        self.controls['canvases']['sketch'].draw()

    # controls
    def set_controls(self):
        controls = self.controls

        # controls
        controls['frames']['controls'] = QFrame()

        # 3d view
        # controls['figs']['3d'] = Figure(tight_layout=True)
        # controls['axes']['3d'] = controls['figs']['3d'].add_subplot(111, projection='3d')
        # controls['canvases']['3d'] = FigureCanvasQTAgg(controls['figs']['3d'])
        controls['frames']['views3d'] = QFrame()
        controls['grids']['views3d'] = QGridLayout()

        # camera view
        controls['figs']['2d'] = []
        controls['axes']['2d'] = []
        controls['frames']['views2d'] = QFrame()
        controls['grids']['views2d'] = QGridLayout()
        controls['plots']['image2d']: Optional[AxesImage] = None

        # sketch view
        controls['figs']['sketch'] = Figure()
        controls['canvases']['sketch'] = FigureCanvasQTAgg(controls['figs']['sketch'])
        controls['labels']['sketch'] = []
        controls['texts']['sketch'] = None

        # sketch zoom view
        controls['labels']['sketch_zoom'] = []

        controls_layout_grid = QGridLayout()
        row = 0
        row = row + 1
        col = 0

        button_load_calibration = QPushButton()
        if self.calibrationIsLoaded:
            button_load_calibration.setStyleSheet("background-color: green;")
        else:
            button_load_calibration.setStyleSheet("background-color: darkred;")
        button_load_calibration.setText('Load Calibration')
        button_load_calibration.clicked.connect(self.button_load_calibration_press)
        controls_layout_grid.addWidget(button_load_calibration, row, col)
        button_load_calibration.setEnabled(self.cfg['button_loadCalibration'])
        controls['buttons']['load_calibration'] = button_load_calibration

        col = 0

        col = col + 1

        label_dx = QLabel()
        label_dx.setText('dx:')
        controls_layout_grid.addWidget(label_dx, row, col)
        controls['labels']['dx'] = label_dx
        col = col + 1

        label_dy = QLabel()
        label_dy.setText('dy:')
        controls_layout_grid.addWidget(label_dy, row, col)
        controls['labels']['dy'] = label_dy
        row = row + 1
        col = 1

        field_dx = QLineEdit()
        field_dx.setValidator(QIntValidator())
        field_dx.setText(str(self.dx))
        controls_layout_grid.addWidget(field_dx, row, col)
        field_dx.returnPressed.connect(self.field_dx_change)
        field_dx.setEnabled(self.cfg['field_dx'])
        controls['fields']['dx'] = field_dx
        col = col + 1

        field_dy = QLineEdit()
        field_dy.setValidator(QIntValidator())
        field_dy.setText(str(self.dy))
        controls_layout_grid.addWidget(field_dy, row, col)
        field_dy.returnPressed.connect(self.field_dy_change)
        field_dy.setEnabled(self.cfg['field_dy'])
        controls['fields']['dy'] = field_dy
        row = row + 1
        col = 0

        col = col + 1

        label_vmin = QLabel()
        label_vmin.setText('vmin:')
        controls_layout_grid.addWidget(label_vmin, row, col)
        controls['labels']['vmin'] = label_vmin
        col = col + 1

        label_vmax = QLabel()
        label_vmax.setText('vmax:')
        controls_layout_grid.addWidget(label_vmax, row, col)
        controls['labels']['vmax'] = label_vmax
        row = row + 1
        col = 0

        col = col + 1

        field_vmin = QLineEdit()
        field_vmin.setValidator(QIntValidator())
        field_vmin.setText(str(self.vmin))
        controls_layout_grid.addWidget(field_vmin, row, col)
        field_vmin.returnPressed.connect(self.field_vmin_change)
        field_vmin.setEnabled(self.cfg['field_vmin'])
        controls['fields']['vmin'] = field_vmin
        col = col + 1

        field_vmax = QLineEdit()
        field_vmax.setValidator(QIntValidator())
        field_vmax.setText(str(self.vmax))
        controls_layout_grid.addWidget(field_vmax, row, col)
        field_vmax.returnPressed.connect(self.field_vmax_change)
        field_vmax.setEnabled(self.cfg['field_vmax'])
        controls['fields']['vmax'] = field_vmax
        row = row + 1
        col = 0

        col += 1
        controls['lists']['cams'] = QComboBox()
        controls['lists']['cams'].addItems(["Camera "+str(i) for i in self.allowed_cams])
        # controls['lists']['cams'].setSizePolicy(QSizePolicy.Expanding,
        #                                          QSizePolicy.Preferred)
        controls['lists']['cams'].currentIndexChanged.connect(self.combo_lists_cams_change)
        controls_layout_grid.addWidget(controls['lists']['cams'], row, col)
        row = row + 1
        col = 0

        button_save_labels = QPushButton()
        button_save_labels.setText('Save Labels (S)')
        button_save_labels.clicked.connect(self.button_save_labels_press)
        controls_layout_grid.addWidget(button_save_labels, row, col)
        button_save_labels.setEnabled(self.cfg['button_saveLabels'])
        controls['buttons']['save_labels'] = button_save_labels
        col = col + 1

        list_labels = QListWidget()
        # list_labels.setSortingEnabled(True)
        list_labels.addItems(self.get_sketch_labels())
        list_labels.setSelectionMode(QAbstractItemView.SingleSelection)
        list_labels.itemClicked.connect(self.list_labels_select)
        controls_layout_grid.addWidget(list_labels, row, col, 3, 2)
        controls['lists']['labels'] = list_labels
        row = row + 1
        col = 0

        row = row + 1

        row = row + 1

        col = col + 1

        button_previous_label = QPushButton()
        button_previous_label.setText('Previous Label (P)')
        button_previous_label.clicked.connect(self.button_previous_label_press)
        controls_layout_grid.addWidget(button_previous_label, row, col)
        button_previous_label.setEnabled(self.cfg['button_previousLabel'])
        controls['buttons']['previous_label'] = button_previous_label
        col = col + 1

        button_next_label = QPushButton()
        button_next_label.setText('Next Label (N)')
        button_next_label.clicked.connect(self.button_next_label_press)
        controls_layout_grid.addWidget(button_next_label, row, col)
        button_next_label.setEnabled(self.cfg['button_nextLabel'])
        controls['buttons']['next_label'] = button_next_label
        row = row + 1
        col = 0

        button_up = QPushButton()
        button_up.setText('Up (\u2191)')
        button_up.clicked.connect(self.button_up_press)
        controls_layout_grid.addWidget(button_up, row, col)
        button_up.setEnabled(self.cfg['button_up'])
        controls['buttons']['up'] = button_up
        col = col + 1

        button_right = QPushButton()
        button_right.setText('Right (\u2192)')
        button_right.clicked.connect(self.button_right_press)
        controls_layout_grid.addWidget(button_right, row, col)
        button_right.setEnabled(self.cfg['button_right'])
        controls['buttons']['right'] = button_right
        col = col + 1

        label_dxyz = QLabel()
        label_dxyz.setText('dxyz:')
        controls_layout_grid.addWidget(label_dxyz, row, col)
        controls['labels']['dxyz'] = label_dxyz
        row = row + 1
        col = 0

        button_down = QPushButton()
        button_down.setText('Down (\u2193)')
        button_down.clicked.connect(self.button_down_press)
        controls_layout_grid.addWidget(button_down, row, col)
        button_down.setEnabled(self.cfg['button_down'])
        controls['buttons']['down'] = button_down
        col = col + 1

        button_left = QPushButton()
        button_left.setText('Left (\u2190)')
        button_left.clicked.connect(self.button_left_press)
        controls_layout_grid.addWidget(button_left, row, col)
        button_left.setEnabled(self.cfg['button_left'])
        controls['buttons']['left'] = button_left

        row = row + 1
        col = 0

        button_previous = QPushButton()
        button_previous.setText('Previous Frame (A)')
        button_previous.clicked.connect(self.button_previous_press)
        controls_layout_grid.addWidget(button_previous, row, col)
        button_previous.setEnabled(self.cfg['button_previous'])
        controls['buttons']['previous'] = button_previous
        col = col + 1

        button_next = QPushButton()
        button_next.setText('Next Frame (D)')
        button_next.clicked.connect(self.button_next_press)
        controls_layout_grid.addWidget(button_next, row, col)
        button_next.setEnabled(self.cfg['button_next'])
        controls['buttons']['next'] = button_next
        col = col + 1

        button_home = QPushButton('Home (H)')
        button_home.clicked.connect(self.button_home_press)
        controls_layout_grid.addWidget(button_home, row, col)
        button_home.setEnabled(self.cfg['button_home'])
        controls['buttons']['home'] = button_home
        row = row + 1
        col = 0

        label_current_pose = QLabel()
        label_current_pose.setText('current frame:')
        controls_layout_grid.addWidget(label_current_pose, row, col)
        controls['labels']['current_pose'] = label_current_pose
        col = col + 1

        label_d_frame = QLabel()
        label_d_frame.setText('dFrame:')
        controls_layout_grid.addWidget(label_d_frame, row, col)
        controls['labels']['d_frame'] = label_d_frame
        row = row + 1
        col = 0

        field_current_pose = QLineEdit()
        field_current_pose.setValidator(QIntValidator())
        field_current_pose.setText(str(self.get_pose_idx()))
        field_current_pose.returnPressed.connect(self.field_current_pose_change)
        controls_layout_grid.addWidget(field_current_pose, row, col)
        field_current_pose.setEnabled(self.cfg['field_currentPose'])
        controls['fields']['current_pose'] = field_current_pose
        col = col + 1

        field_d_frame = QLineEdit()
        field_d_frame.setValidator(QIntValidator())
        field_d_frame.setText(str(self.dFrame))
        field_d_frame.returnPressed.connect(self.field_d_frame_change)
        controls_layout_grid.addWidget(field_d_frame, row, col)
        field_d_frame.setEnabled(self.cfg['field_dFrame'])
        controls['fields']['d_frame'] = field_d_frame
        row = row + 1
        col = 0

        button_zoom = QPushButton('Zoom (Z)')
        button_zoom.setCheckable(True)
        update_button_stylesheet(button_zoom)
        button_zoom.clicked.connect(self.button_zoom_press)
        controls_layout_grid.addWidget(button_zoom, row, col)
        button_zoom.setEnabled(self.cfg['button_zoom'])
        controls['buttons']['zoom'] = button_zoom
        col = col + 1

        button_pan = QPushButton('Pan (W)')
        button_pan.setCheckable(True)
        update_button_stylesheet(button_pan)
        button_pan.clicked.connect(self.button_pan_press)
        controls_layout_grid.addWidget(button_pan, row, col)
        controls['buttons']['pan'] = button_pan
        col = col + 1

        button_rotate = QPushButton('Rotate (R)')
        button_rotate.clicked.connect(self.button_rotate_press)
        controls_layout_grid.addWidget(button_rotate, row, col)
        controls['buttons']['rotate'] = button_rotate
        controls['frames']['controls'].setLayout(controls_layout_grid)
        row = row + 1
        col = 0

        label_dxyz = QLabel()
        label_dxyz.setText('')
        controls_layout_grid.addWidget(label_dxyz, row, col)
        controls['labels']['labeler'] = label_dxyz

        self.controls = controls

    def button_load_calibration_press(self):
        dialog = QFileDialog()
        dialog.setStyleSheet("background-color: white;")
        dialog_options = dialog.Options()
        dialog_options |= dialog.DontUseNativeDialog
        file_name, _ = QFileDialog.getOpenFileName(dialog,
                                                   "Choose calibration file",
                                                   ""
                                                   "npy files (*.npy)",
                                                   options=dialog_options)
        if file_name:
            self.load_calibrations(Path(file_name))
            print('Loaded calibration ({:s})'.format(file_name))
        self.controls['buttons']['load_calibration'].clearFocus()

    def list_labels_select(self):
        self.trigger_autosave_event()
        self.plot2d_update()
        self.sketch_update()
        self.controls['lists']['labels'].clearFocus()

    def trigger_autosave_event(self):
        if self.cfg['autoSave'] and not self.master:
            self.autoSaveCounter = self.autoSaveCounter + 1
            if np.mod(self.autoSaveCounter, self.cfg['autoSaveN0']) == 0:
                file = self.standardLabelsFolder / 'labels.yml'  # this is equal to self.standardLabelsFile
                label_lib.save(file, self.labels)
                print('Automatically saved labels ({:s})'.format(file.as_posix()))
            if np.mod(self.autoSaveCounter, self.cfg['autoSaveN1']) == 0:
                file = self.standardLabelsFolder / 'autosave' / 'labels.yml'
                label_lib.save(file, self.labels)
                print('Automatically saved labels ({:s})'.format(file.as_posix()))
                #
                self.autoSaveCounter = 0

    def field_dx_change(self):
        try:
            int(self.controls['fields']['dx'].text())
            field_input_is_correct = True
        except ValueError:
            field_input_is_correct = False
        if field_input_is_correct:
            self.dx = int(np.max([8, int(self.controls['fields']['dx'].text())]))
        self.controls['fields']['dx'].setText(str(self.dx))
        self.plot2d_draw()
        self.controls['fields']['dx'].clearFocus()

    def field_dy_change(self):
        try:
            int(self.controls['fields']['dy'].text())
            field_input_is_correct = True
        except ValueError:
            field_input_is_correct = False
        if field_input_is_correct:
            self.dy = int(np.max([8, int(self.controls['fields']['dy'].text())]))
        self.controls['fields']['dy'].setText(str(self.dy))
        self.plot2d_draw()
        self.controls['fields']['dy'].clearFocus()

    def field_vmin_change(self):
        try:
            int(self.controls['fields']['vmin'].text())
            field_input_is_correct = True
        except ValueError:
            field_input_is_correct = False
        if field_input_is_correct:
            self.vmin = int(self.controls['fields']['vmin'].text())
            self.vmin = int(np.max([0, self.vmin]))
            self.vmin = int(np.min([self.vmin, 254]))
            self.vmin = int(np.min([self.vmin, self.vmax - 1]))
        else:
            for i_cam in range(len(self.cameras)):
                self.controls['plots']['image2d'].set_clim(self.vmin, self.vmax)
                self.controls['figs']['2d'].canvas.draw()
        self.controls['fields']['vmin'].setText(str(self.vmin))
        self.controls['fields']['vmin'].clearFocus()

    def field_vmax_change(self):
        try:
            int(self.controls['fields']['vmax'].text())
            field_input_is_correct = True
        except ValueError:
            field_input_is_correct = False
        if field_input_is_correct:
            self.vmax = int(self.controls['fields']['vmax'].text())
            self.vmax = int(np.max([1, self.vmax]))
            self.vmax = int(np.min([self.vmax, 255]))
            self.vmax = int(np.max([self.vmin + 1, self.vmax]))
            self.controls['plots']['image2d'].set_clim(self.vmin, self.vmax)
            self.controls['figs']['2d'].canvas.draw()
        else:
            for i_cam in range(len(self.cameras)):
                self.controls['plots']['image2d'].set_clim(self.vmin, self.vmax)
                self.controls['figs']['2d'].canvas.draw()
        self.controls['fields']['vmax'].setText(str(self.vmax))
        self.controls['fields']['vmax'].clearFocus()

    def button_home_press(self):
        if self.recordingIsLoaded:
            self.zoom_reset()
            self.disable_pan()
            self.disable_zoom()

            self.plot2d_draw()

            self.controls['toolbars']['views2d'].home()
        self.controls['buttons']['home'].clearFocus()

    def button_zoom_press(self):
        if self.recordingIsLoaded:
            update_button_stylesheet(self.controls['buttons']['zoom'])
            self.disable_pan()
            self.controls['toolbars']['views2d'].zoom()
        else:
            print('WARNING: Recording needs to be loaded first')
        self.controls['buttons']['zoom'].clearFocus()

    def disable_pan(self):
        if get_button_status(self.controls['buttons']['pan']):
            disable_button(self.controls['buttons']['pan'])
            self.controls['toolbars']['views2d'].pan()

    def button_rotate_press(self):
        if self.recordingIsLoaded:
            self.cameras[self.camera_idx]["rotate"] = not self.cameras[self.camera_idx]["rotate"]

            self.controls['axes']['2d'].invert_xaxis()
            self.controls['axes']['2d'].invert_yaxis()
            self.controls['figs']['2d'].canvas.draw()
        else:
            print('WARNING: Recording needs to be loaded first')
        self.controls['buttons']['pan'].clearFocus()

    def button_pan_press(self):
        if self.recordingIsLoaded:
            update_button_stylesheet(self.controls['buttons']['pan'])
            self.disable_zoom()
            self.controls['toolbars']['views2d'].pan()
        else:
            print('WARNING: Recording needs to be loaded first')
        self.controls['buttons']['pan'].clearFocus()

    def disable_zoom(self):
        if get_button_status(self.controls['buttons']['zoom']):
            disable_button(self.controls['buttons']['zoom'])
            self.controls['toolbars']['views2d'].zoom()

    def sketch_click(self, event):
        if event.button == 1:
            x = event.xdata
            y = event.ydata
            if (x is not None) & (y is not None):
                label_coordinates = self.get_sketch_label_coordinates()
                dists = ((x - label_coordinates[:, 0]) ** 2 + (y - label_coordinates[:, 1]) ** 2) ** 0.5
                label_index = np.argmin(dists)
                self.controls['lists']['labels'].setCurrentRow(label_index)
                self.list_labels_select()

    # this works correctly but implementation is somewhat messed up
    # (rows are dimensions and not the columns, c.f. commented plotting command)
    def plot3d_move_center(self, move_direc):
        return
        # x_lim = self.controls['axes']['3d'].get_xlim()
        # y_lim = self.controls['axes']['3d'].get_ylim()
        # z_lim = self.controls['axes']['3d'].get_zlim()
        # center = np.array([np.mean(x_lim),
        #                    np.mean(y_lim),
        #                    np.mean(z_lim)], dtype=np.float64)
        # dxzy_lim = np.mean([np.abs(center[0] - x_lim),
        #                     np.abs(center[1] - y_lim),
        #                     np.abs(center[2] - z_lim)])
        #
        # azim = self.controls['axes']['3d'].azim / 180 * np.pi + np.pi
        # elev = self.controls['axes']['3d'].elev / 180 * np.pi
        #
        # r_azim = np.array([0.0, 0.0, -azim], dtype=np.float64)
        # R_azim = rodrigues2rotmat_single(r_azim)
        #
        # r_elev = np.array([0.0, -elev, 0.0], dtype=np.float64)
        # R_elev = rodrigues2rotmat_single(r_elev)
        #
        # R_azim_elev = np.dot(R_elev, R_azim)
        #
        # #        coord = center + R_azim_elev * 0.1
        # #        label = ['x', 'y', 'z']
        # #        for i in range(3):
        # #            self.controls['axes']['3d'].plot([center[0], coord[i, 0]],
        # #                           [center[1], coord[i, 1]],
        # #                           [center[2], coord[i, 2]],
        # #                           linestyle='-',
        # #                           color='green')
        # #            self.controls['axes']['3d'].text(coord[i, 0],
        # #                           coord[i, 1],
        # #                           coord[i, 2],
        # #                           label[i],
        # #                           color='red')
        #
        # center_new = center + np.sign(move_direc) * R_azim_elev[np.abs(move_direc), :] * self.dxyz
        # self.controls['axes']['3d'].set_xlim([center_new[0] - dxzy_lim,
        #                                       center_new[0] + dxzy_lim])
        # self.controls['axes']['3d'].set_ylim([center_new[1] - dxzy_lim,
        #                                       center_new[1] + dxzy_lim])
        # self.controls['axes']['3d'].set_zlim([center_new[2] - dxzy_lim,
        #                                       center_new[2] + dxzy_lim])
        # self.controls['canvases']['3d'].draw()

    def button_up_press(self):
        move_direc = +2
        self.plot3d_move_center(move_direc)
        self.controls['buttons']['up'].clearFocus()

    def button_down_press(self):
        move_direc = -2
        self.plot3d_move_center(move_direc)
        self.controls['buttons']['down'].clearFocus()

    def button_left_press(self):
        move_direc = +1
        self.plot3d_move_center(move_direc)
        self.controls['buttons']['left'].clearFocus()

    def button_right_press(self):
        move_direc = -1
        self.plot3d_move_center(move_direc)
        self.controls['buttons']['right'].clearFocus()

    def button_next_label_press(self):
        self.change_label(1)

    def button_previous_label_press(self):
        self.change_label(-1)

    def change_label(self, d_label_idx):
        selected_label_name = self.get_current_label()
        label_keys = list(self.get_sketch_labels().keys())
        selected_label_index = label_keys.index(selected_label_name)
        next_label_index = selected_label_index + d_label_idx
        if next_label_index >= np.size(label_keys):
            next_label_index = 0
        elif next_label_index < 0:
            next_label_index = np.size(label_keys) - 1
        self.controls['lists']['labels'].setCurrentRow(next_label_index)
        self.list_labels_select()
        self.controls['buttons']['next_label'].clearFocus()

    def button_next_press(self):
        modifiers = QApplication.keyboardModifiers()
        nextframe = self.get_pose_idx() + self.dFrame
        if modifiers == QtCore.Qt.AltModifier:
            self.autolabel(nextframe)
        self.set_pose_idx(nextframe)

    def button_previous_press(self):
        modifiers = QApplication.keyboardModifiers()
        nextframe = self.get_pose_idx() - self.dFrame
        if modifiers == QtCore.Qt.AltModifier:
            self.autolabel(nextframe)
        self.set_pose_idx(nextframe)

    def autolabel(self, nextframe, radius=10, coords = None):
        fr_idx = self.get_pose_idx()
        label_name = self.get_current_label()
        cam_idx = self.camera_idx
        reader = self.cameras[cam_idx]["reader"]
        img = np.linalg.norm(reader.get_data(nextframe), axis=2)
        if coords is None:
            coords = self.labels['labels'][label_name][fr_idx][cam_idx]
            other = self.labels['labels'][label_name][2 * fr_idx - nextframe][cam_idx]
            if other is not None:
                coords = 2 * coords - other
        import scipy
        img = img.astype(np.uint16)
        img = scipy.ndimage.convolve1d(img, [1, 2, 3, 4, 3, 2, 1], axis=0)
        img = scipy.ndimage.convolve1d(img, [1, 2, 3, 4, 3, 2, 1], axis=1)
        xx, yy = np.meshgrid(np.arange(img.shape[1]), np.arange(img.shape[0]))
        mask = (xx - coords[0]) ** 2 + (yy - coords[1]) ** 2 < radius ** 2
        mindex = np.argmax(img * mask)
        mindex = np.unravel_index(mindex, img.shape)
        self.add_label((mindex[1], mindex[0]), label_name, cam_idx, nextframe)

    def field_current_pose_change(self):
        try:
            current_pose_idx = int(self.controls['fields']['current_pose'].text())
        except ValueError:
            print(f"Invalid pose {self.controls['fields']['current_pose'].text()}")
            return

        self.set_pose_idx(current_pose_idx)
        self.controls['fields']['current_pose'].setText(str(self.get_pose_idx()))
        self.list_labels_select()
        self.sketch_update()
        self.controls['fields']['current_pose'].clearFocus()

    def field_d_frame_change(self):
        try:
            int(self.controls['fields']['d_frame'].text())
            field_input_is_correct = True
        except ValueError:
            field_input_is_correct = False
        if field_input_is_correct:
            self.dFrame = int(np.max([1, int(self.controls['fields']['d_frame'].text())]))
            self.zoom_reset()
        self.controls['fields']['d_frame'].setText(str(self.dFrame))
        self.controls['fields']['d_frame'].clearFocus()

    def zoom_reset(self):
        x_res = self.get_x_res()
        y_res = self.get_y_res()
        for i_cam in range(len(self.cameras)):
            self.cameras[i_cam]['x_lim_prev'] = np.array([0.0, x_res[i_cam] - 1], dtype=np.float64)
            self.cameras[i_cam]['y_lim_prev'] = np.array([0.0, y_res[i_cam] - 1], dtype=np.float64)

    def plot2d_change_frame(self):
        for i_cam in range(len(self.cameras)):
            self.cameras[i_cam]['x_lim_prev'] = self.controls['axes']['2d'].get_xlim()
            self.cameras[i_cam]['y_lim_prev'] = self.controls['axes']['2d'].get_ylim()  # [::-1]

        if "labeler" in self.controls['labels']:  # MIght not yet be initialized ...
            self.controls['labels']['labeler'].setText(
                ", ".join(label_lib.get_frame_labelers(self.labels, self.get_pose_idx(), cam_idx=self.camera_idx))
            )

        if len(self.controls['fields'].items())==0:  # we are not yet initialized. probably unnecessary after cleanup
            return

        self.controls['fields']['current_pose'].setText(str(self.get_pose_idx()))
        self.plot2d_update_image(self.controls['axes']['2d'], self.camera_idx)
        self.list_labels_select()
        # self.sketch_update()
        # self.plot2d_draw()

        for i_cam in range(len(self.cameras)):
            self.controls['axes']['2d'].set_xlim(self.cameras[i_cam]['x_lim_prev'])
            self.controls['axes']['2d'].set_ylim(self.cameras[i_cam]['y_lim_prev'])  # [::-1]
        self.controls['figs']['2d'].canvas.draw()

    def button_save_labels_press(self):
        if self.master:
            dialog = QFileDialog()
            dialog.setStyleSheet("background-color: white;")
            dialog_options = dialog.Options()
            dialog_options |= dialog.DontUseNativeDialog
            print(self.standardLabelsFile)
            file_name, _ = QFileDialog.getSaveFileName(dialog,
                                                       "Save labels file",
                                                       os.path.dirname(self.standardLabelsFile),
                                                       "npz files (*.npz)",
                                                       options=dialog_options)

            if file_name:
                file_name = Path(file_name)
        else:
            file_name = self.standardLabelsFile

        label_lib.save(file_name, self.labels)
        self.controls['buttons']['save_labels'].clearFocus()

    def closeEvent(self, event):
        if self.cfg['exitSaveModel']:
            self.button_save_model_press()
        if self.cfg['exitSaveLabels']:
            self.button_save_labels_press()

        if not self.master:
            exit_status = dict()
            exit_status['i_pose'] = self.get_pose_idx()
            np.save(self.standardLabelsFolder / 'exit_status.npy', exit_status)

    # shortkeys
    def keyPressEvent(self, event):
        if not (event.isAutoRepeat()):
            if self.cfg['button_next'] and event.key() == Qt.Key_D:
                self.button_next_press()
            elif self.cfg['button_previous'] and event.key() == Qt.Key_A:
                self.button_previous_press()
            elif self.cfg['button_nextLabel'] and event.key() == Qt.Key_N:
                self.button_next_label_press()
            elif self.cfg['button_previousLabel'] and event.key() == Qt.Key_P:
                self.button_previous_label_press()
            elif self.cfg['button_home'] and event.key() == Qt.Key_H:
                self.button_home_press()
            elif self.cfg['button_zoom'] and event.key() == Qt.Key_Z:
                toggle_button(self.controls['buttons']['zoom'])
                self.button_zoom_press()
            elif self.cfg['button_pan'] and event.key() == Qt.Key_W:
                toggle_button(self.controls['buttons']['pan'])
                self.button_pan_press()
            elif self.cfg['button_pan'] and event.key() == Qt.Key_R:
                self.button_rotate_press()
            elif self.cfg['button_label3d'] and event.key() == Qt.Key_L:
                pass
                # self.button_label3d_press()
            elif self.cfg['button_up'] and event.key() == Qt.Key_Up:
                self.button_up_press()
            elif self.cfg['button_down'] and event.key() == Qt.Key_Down:
                self.button_down_press()
            elif self.cfg['button_left'] and event.key() == Qt.Key_Left:
                self.button_left_press()
            elif self.cfg['button_right'] and event.key() == Qt.Key_Right:
                self.button_right_press()
            elif self.cfg['button_saveLabels'] and event.key() == Qt.Key_S:
                self.button_save_labels_press()
            elif self.cfg['field_vmax'] and event.key() == Qt.Key_Plus:
                self.controls['fields']['vmax'].setText(str(int(self.vmax * 0.8)))
                self.field_vmax_change()
            elif self.cfg['field_vmax'] and event.key() == Qt.Key_Minus:
                self.controls['fields']['vmax'].setText(str(int(self.vmax / 0.8)))
                self.field_vmax_change()
            elif event.key() == Qt.Key_1:
                if len(self.cameras) > 0:
                    self.controls['lists']['fast_labeling_mode'].setCurrentIndex(0)
            elif event.key() == Qt.Key_2:
                if len(self.cameras) > 1:
                    self.controls['lists']['fast_labeling_mode'].setCurrentIndex(1)
            elif event.key() == Qt.Key_3:
                if len(self.cameras) > 2:
                    self.controls['lists']['fast_labeling_mode'].setCurrentIndex(2)
            elif event.key() == Qt.Key_4:
                if len(self.cameras) > 3:
                    self.controls['lists']['fast_labeling_mode'].setCurrentIndex(3)
            elif event.key() == Qt.Key_5:
                if len(self.cameras) > 4:
                    self.controls['lists']['fast_labeling_mode'].setCurrentIndex(4)
            elif event.key() == Qt.Key_6:
                if len(self.cameras) > 5:
                    self.controls['lists']['fast_labeling_mode'].setCurrentIndex(5)

        else:
            print('WARNING: Auto-repeat is not supported')


# noinspection PyUnusedLocal
def main(drive: Path, config_file=None, master=True, sync=False):
    app = QApplication(sys.argv)
    window = MainWindow(drive=drive, file_config=config_file, master=master, sync=sync)
    sys.exit(app.exec_())


if __name__ == '__main__':
    main(Path('o:/analysis/'))


class UnsupportedFormatException(Exception):
    pass
