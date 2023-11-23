import os
from pathlib import Path

from PyQt5.QtGui import QGuiApplication
from PyQt5.QtWidgets import QDialog, QGridLayout, QComboBox, QSizePolicy, QPushButton

from glob import glob

import yaml

class SelectUserWindow(QDialog):
    def __init__(self, drive: Path, parent=None):
        super(SelectUserWindow, self).__init__(parent)
        self.drive = drive

        self.setGeometry(0, 0, 256, 128)
        self.center()
        self.setWindowTitle('Select User')

        self.user_list = self.get_user_list(drive)
        self.job_names = []

        self.selecting_layout = QGridLayout()

        self.user_combobox = QComboBox()
        self.user_combobox.addItems(self.user_list)
        self.user_combobox.setSizePolicy(QSizePolicy.Expanding,
                                         QSizePolicy.Preferred)
        self.selecting_layout.addWidget(self.user_combobox)

        self.job_combobox = QComboBox()
        self.job_combobox.setDisabled(True)
        self.job_combobox.setSizePolicy(QSizePolicy.Expanding,
                                         QSizePolicy.Preferred)
        self.selecting_layout.addWidget(self.job_combobox)

        self.selecting_button = QPushButton('Ok')
        self.selecting_button.setSizePolicy(QSizePolicy.Expanding,
                                            QSizePolicy.Preferred)
        self.selecting_layout.addWidget(self.selecting_button)

        self.user_combobox.currentIndexChanged.connect(self.user_change)
        self.selecting_button.clicked.connect(self.accept)

        self.setLayout(self.selecting_layout)

        self.defaults_file = Path("~/.bbo_labelgui/defaults.yml").expanduser().resolve()

        default_config = self.read_defaults()
        if default_config["user"] in self.user_list:
            self.user_combobox.setCurrentIndex(self.user_list.index(default_config["user"]))

        if default_config["job"] in self.job_names:
            self.job_combobox.setCurrentIndex(self.job_names.index(default_config["job"]))

    def read_defaults(self):
        default_config = None
        if self.defaults_file.is_file():
            with open(self.defaults_file, 'r') as fh:
                default_config = yaml.safe_load(fh)

        if default_config is None or not ('user' in default_config and 'job' in default_config):
            default_config = {
                'user': None,
                'job': None,
            }
        return default_config

    def write_defaults(self, user=None, job=None):
        if user is None:
            user = self.get_user()
            job = self.get_job()

        default_config = self.read_defaults()
        default_config["user"] = user
        default_config["job"] = job

        os.makedirs(self.defaults_file.parent, exist_ok=True)
        with open(self.defaults_file, 'w') as fh:
            yaml.safe_dump(default_config, fh)


    @staticmethod
    def get_user_list(drive):
        user_list = sorted(os.listdir(drive / 'data' / 'user'))
        return user_list

    def user_change(self):
        job_dir = self.drive / 'data' / 'user' / self.get_user() / 'jobs'
        if job_dir.is_dir():
            jobs = glob((job_dir / '*.py').as_posix())
            if len(jobs)>0:
                jobs = sorted(jobs)
                self.job_combobox.setDisabled(False)
                self.job_names = [Path(j).stem for j in jobs]
                self.job_combobox.clear()
                self.job_combobox.addItems(self.job_names)
            else:
                self.job_combobox.clear()
                self.job_combobox.setDisabled(True)
        else:
            self.job_combobox.clear()
            self.job_combobox.setDisabled(True)

    def center(self):
        qr = self.frameGeometry()
        cp = QGuiApplication.primaryScreen().geometry().center()
        qr.moveCenter(cp)
        self.move(qr.topLeft())

    def get_user(self):
        user_id = self.user_combobox.currentIndex()
        user = self.user_list[user_id]
        return user

    def get_job(self):
        job_id = self.job_combobox.currentIndex()
        if job_id != -1:
            job = self.job_names[job_id]
        else:
            job = None
        return job

    @staticmethod
    def start(drive, parent=None):
        selecting = SelectUserWindow(drive=drive, parent=parent)
        exit_sel = selecting.exec_()
        user = selecting.get_user()
        job = selecting.get_job()
        selecting.write_defaults(user, job)


        return user, job, exit_sel == QDialog.Accepted
