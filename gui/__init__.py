import os
import sys
import time
import logging
import platform
from typing import NamedTuple, Optional
from pathlib import Path
from ovl_util import logs, config
from gui.widgets import MainWindow

from PyQt5.QtCore import Qt, qVersion
from PyQt5.QtWidgets import QApplication, QStyleFactory
from PyQt5.QtGui import QPalette


def check_python() -> None:
	"""Require Python == 3.11"""
	is_64bits = sys.maxsize > 2 ** 32
	if not is_64bits:
		logging.warning(
			"Either your operating system or your python installation is not 64 bits. "
			"Large OVLs will crash unexpectedly!")
	if (sys.version_info.major, sys.version_info.minor) != (3, 11):
		logging.critical("Python 3.11 is required. Please change your Python installation.")
		time.sleep(60)


class GuiOptions(NamedTuple):
	log_name: str
	log_to_file: bool = True
	log_to_stdout: bool = True
	log_backup_count: int = 4
	qapp: Optional[QApplication] = None
	frameless: bool = True
	style: str = "Fusion"
	palette: str = "dark"
	qss_file: str = ""
	stylesheet: str = R"""
		QToolTip { color: #ffffff; background-color: #353535; border: 1px solid white; }
	"""


def init(cls: type[MainWindow], opts: GuiOptions) -> tuple[MainWindow, QApplication]:
	"""Initialize the window class, logs, and QApplication if necessary"""
	handler = logs.logging_setup(opts.log_name,
								 log_to_file=opts.log_to_file,
								 log_to_stdout=opts.log_to_stdout,
								 backup_count=opts.log_backup_count)
	check_python()
	app = opts.qapp
	if app is None:
		if qVersion().startswith("5."):
			QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
			QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling)
			QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)
		app = QApplication([])
	win = cls(opts=opts)
	win.set_log_name(opts.log_name)
	win.stdout_handler = handler
	win.activateWindow()
	return win, app


def startup(cls: type[MainWindow], opts: GuiOptions) -> None:
	"""Startup the window, set the theme, handle config and logs on application exit"""
	if platform.system() == "Windows":
		import ctypes
		myappid = 'Open Naja OVL Tools' # arbitrary string
		ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
	win, app_qt = init(cls, opts)
	win.show()
	if not win.cfg.get("light_theme", False):
		from gui import qt_theme
		app_qt.setStyle(QStyleFactory.create(opts.style))
		app_qt.setPalette(qt_theme.palettes.get(opts.palette, QPalette()))
		if opts.qss_file:
			with open(opts.qss_file,"r") as qss:
				app_qt.setStyleSheet(qss.read())
		elif opts.stylesheet:
			app_qt.setStyleSheet(opts.stylesheet)
	app_qt.exec_()
	config.save_config(win.cfg)
	logging.shutdown()
