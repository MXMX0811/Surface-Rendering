import sys
import numpy as np
from PyQt5.QtWidgets import QApplication, QWidget, QSlider, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
from PyQt5.QtCore import pyqtSignal, pyqtSlot, Qt, QThread
from PyQt5.QtGui import QPainter, QPen, QPainterPath, QPixmap
from PyQt5 import QtGui
from pyautd3.link import SOEM, OnLostFunc
from pyautd3.link import Simulator
from pyautd3.gain import Focus
from pyautd3 import Controller, Geometry, SilencerConfig, Clear, Synchronize, Stop, DEVICE_WIDTH, DEVICE_HEIGHT
from pyautd3.modulation import Sine
from datetime import timedelta
import time
import math
import pySequentialLineSearch
import pyrealsense2 as rs
import cv2
import os
import ctypes
import platform


# drawing the waveform
class SinusoidWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 200)
        self._amplitude = 1.0
        self._frequency = 1.0
        self._offset = 0.0
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(self.backgroundRole(), Qt.white)
        self.setPalette(palette)

    def setAmplitude(self, amplitude):
        self._amplitude = amplitude
        self.update()

    def setOffset(self, offset):
        self._offset = offset
        self.update()

    def setFrequency(self, frequency):
        self._frequency = frequency
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(Qt.blue, 2))

        width = self.width()
        height = self.height()
        x_scale = width / (0.3 * math.pi)
        y_scale = height

        path = QPainterPath()
        path.moveTo(0, height / 2)

        line_thickness = 3
        painter.setPen(QPen(Qt.blue, line_thickness))
        for x in range(width):
            t = x / x_scale
            y = 0.5 * self._amplitude * math.sin(self._frequency * t) + self._offset
            path.lineTo(x, height - y * y_scale)
        painter.drawPath(path)

        # draw the axis
        axis_thickness = 10
        painter.setPen(QPen(Qt.black, axis_thickness))
        painter.drawLine(0, height, width, height)
        painter.drawLine(0, 0, 0, height)


# AUTD thread
class AUTDThread(QThread):
    SLS_para_signal = pyqtSignal(np.ndarray)
    position_signal = pyqtSignal(np.ndarray)

    def __init__(self):
        super().__init__()
        # connect the signal to the slot function
        self.SLS_para_signal.connect(self.SLSSignal)
        self.video_thread = VideoThread()
        self.video_thread.position_signal.connect(self.PositionSignal)

        self._run_flag = True

        # initial parameters
        self.coordinate = np.array([0., 0., 230.])
        self.m = Sine(100)

        # import the HighPrecisionSleep() method
        dll = ctypes.cdll.LoadLibrary
        self.libc = dll(os.path.dirname(__file__) + '/../cpp/' + platform.system().lower() +\
                         '/HighPrecisionTimer.so') 

    # slot function to accept SLS parameters
    @pyqtSlot(np.ndarray)
    def SLSSignal(self, SLS_para):
        self.stm_f = SLS_para[0]
        self.radius = SLS_para[1]
        freq = SLS_para[2]
        amp = SLS_para[3]
        offset = -0.5 * amp + 1
        self.m = Sine(freq=int(freq), amp=amp, offset=offset)
    
    # slot function to accept coordinates
    @pyqtSlot(np.ndarray)
    def PositionSignal(self, coordinate):
        self.coordinate = coordinate
    
    def on_lost(self, msg: ctypes.c_char_p):
        print(msg.decode('utf-8'), end="")
        os._exit(-1)

    def stop(self):
        # set run flag to False and waits for thread to finish
        self._run_flag = False
        self.wait()

    def run(self):
        geometry = Geometry.Builder()\
            .add_device([-DEVICE_WIDTH / 2, DEVICE_HEIGHT / 2 + 12.5, 0.], [0., 0., 0.])\
            .add_device([DEVICE_WIDTH / 2, DEVICE_HEIGHT / 2 + 12.5, 0.], [0., 0., 0.])\
            .add_device([-DEVICE_WIDTH / 2, -DEVICE_HEIGHT / 2 - 12.5, 0.], [0., 0., 0.])\
            .add_device([DEVICE_WIDTH / 2, -DEVICE_HEIGHT / 2 - 12.5, 0.], [0., 0., 0.])\
            .build()

        link = Simulator().build()
        on_lost_func = OnLostFunc(self.on_lost)
        # link = SOEM().on_lost(on_lost_func).build()

        autd = Controller.open(geometry, link)

        autd.send(Clear())
        autd.send(Synchronize())

        print('================================== Firmware information ====================================')
        firm_info_list = autd.firmware_info_list()
        for firm in firm_info_list:
            print(firm)
        print('============================================================================================')

        center = autd.geometry.center + np.array([0., 0., 0.])

        time_step = 0.002
        theta = 0
        config = SilencerConfig()
        autd.send(config)

        print('press ctrl+c to finish...')

        try:
            while self._run_flag:
                stm_f = self.stm_f
                radius = self.radius

                # ... change the radius and height here
                x = self.coordinate[0]
                y = self.coordinate[1]
                # D435i depth start point: -4.2 mm
                # the height difference between the transducer surface and the camera: 9 mm
                height = self.coordinate[2] - 9 - 4.2
                
                # update the focus information
                p = radius * np.array([np.cos(theta), np.sin(theta), 0])
                p += np.array([x, y, height])
                f = Focus(center + p)
                tic = time.time()
                autd.send(self.m, f, timedelta(microseconds=0))
                toc = time.time()
                print(toc-tic)

                theta += 2 * np.pi * stm_f * time_step

                tic = time.time()
                self.libc.HighPrecisionSleep(ctypes.c_float(time_step))  # cpp sleep function
                toc = time.time()
                print(toc-tic)

        except KeyboardInterrupt:
            pass

        print('finish.')
        autd.send(Stop())
        autd.dispose()


class VideoThread(QThread):
    change_pixmap_signal = pyqtSignal(np.ndarray)
    position_signal = pyqtSignal(np.ndarray)

    def __init__(self):
        super().__init__()
        self._run_flag = True

        self.pipeline = rs.pipeline()
        self.config = rs.config()
        self.config.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 30)

    def run(self):
        # Start streaming
        self.pipeline.start(self.config)

        while self._run_flag:
            frames = self.pipeline.wait_for_frames()
            depth_frame = frames.get_depth_frame()
            if not depth_frame:
                continue
            
            W = depth_frame.get_width()
            H = depth_frame.get_height()
            # the height range: 0 ~ 23 cm
            filter = rs.threshold_filter(min_dist=0, max_dist=0.23)
            depth_frame = filter.process(depth_frame)
            depth_img = np.asanyarray(depth_frame.get_data())
            # the contact area, 100 x 100 pix
            depth_img = depth_img[int(H/2)-50:int(H/2)+50, int(W/2)-50:int(W/2)+50]

            mass_x, mass_y = np.where(depth_img > 0)

            # if no depth infomation, continue
            if mass_x.size == 0 or mass_y.size == 0:
                depth_img = cv2.applyColorMap(cv2.convertScaleAbs(depth_img), cv2.COLORMAP_JET)
                self.change_pixmap_signal.emit(depth_img)
                continue

            # mass_x and mass_y are the list of x indices and y indices of mass pixels
            # calculate the centroid
            cent_x = int(np.average(mass_x))
            cent_y = int(np.average(mass_y))
            # print(cent_x, cent_y)
            height = depth_img[cent_x, cent_y]

            # depth fov of D435i: 87° x 58°
            # rgb fov of D435i: 69° x 42°
            # calculate the coodinate using the fov
            ang_x = math.radians((cent_x - 50) / (W / 2) * (87 / 2))
            ang_y = math.radians((cent_y - 50) / (H / 2) * (58 / 2))
            x_dis = math.tan(ang_x) * height
            y_dis = math.tan(ang_y) * height

            # print('X:', x_dis, 'Y:', y_dis, 'Z:', height)
            # send the coodinate signal
            self.position_signal.emit(np.array([y_dis, x_dis, height]))
            
            # draw the rendering area
            cv2.circle(depth_img, (cent_y, cent_x), 5, (255, 255, 255), -1)
            depth_img = cv2.applyColorMap(cv2.convertScaleAbs(depth_img), cv2.COLORMAP_JET)
            self.change_pixmap_signal.emit(depth_img)

    def stop(self):
        # set run flag to False and waits for thread to finish
        self._run_flag = False
        self.wait()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sequential Line Search")
        self.video_thread = VideoThread()
        self.autd_thread = AUTDThread()

        self.image_disp_w_h = 320

        self.image_label = QLabel(self)
        self.image_label.resize(self.image_disp_w_h, self.image_disp_w_h)

        whole_hbox = QHBoxLayout()
        whole_hbox.addWidget(self.image_label)

        self.horizontal_slider = QSlider(Qt.Horizontal)
        self.horizontal_slider.setRange(0, 999)
        self.horizontal_slider.setSliderPosition(0)

        self.vertical_sliders = []

        self.sinusoid_widget = SinusoidWidget()

        layout = QVBoxLayout()
        layout.addWidget(self.sinusoid_widget)

        horizontal_layout = QHBoxLayout()
        labels = ["f_STM", "radius", "f_wave", "amplitude"]
        for i in range(4):
            vertical_slider = QSlider(Qt.Vertical)
            vertical_slider.setRange(0, 100)
            vertical_slider.setEnabled(False)
            self.vertical_sliders.append(vertical_slider)

            label = QLabel(labels[i])

            vertical_box = QVBoxLayout()
            vertical_box.addWidget(label, 1, Qt.AlignCenter | Qt.AlignTop)
            vertical_box.addWidget(vertical_slider, 0, Qt.AlignCenter | Qt.AlignTop)

            horizontal_layout.addLayout(vertical_box)

        layout.addLayout(horizontal_layout)
        layout.addWidget(self.horizontal_slider)

        self.optimizer = pySequentialLineSearch.SequentialLineSearchOptimizer(num_dims=4)

        self.optimizer.set_hyperparams(kernel_signal_var=0.50,
                                kernel_length_scale=0.10,
                                kernel_hyperparams_prior_var=0.10)
        
        self.optimizer.set_gaussian_process_upper_confidence_bound_hyperparam(5.)

        self.horizontal_slider.valueChanged.connect(lambda value: self.updateValues())

        next_button = QPushButton("Next")
        next_button.clicked.connect(lambda value: self.updateOptimizer())
        layout.addWidget(next_button)

        whole_hbox.addLayout(layout)
        self.setLayout(whole_hbox)

        self.updateValues()
        # connect its signal to the update_image slot
        self.video_thread.change_pixmap_signal.connect(self.update_image)
        # start the thread
        self.video_thread.start()
        self.autd_thread.start()

    def closeEvent(self, event):
        self.video_thread.stop()
        self.autd_thread.stop()
        event.accept()

    @pyqtSlot(np.ndarray)
    def update_image(self, cv_img):
        """Updates the image_label with a new opencv image"""
        qt_img = self.convert_cv_qt(cv_img)
        self.image_label.setPixmap(qt_img)
    
    def convert_cv_qt(self, cv_img):
        """Convert from an opencv image to QPixmap"""
        rgb_image = cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb_image.shape
        bytes_per_line = ch * w
        convert_to_Qt_format = QtGui.QImage(rgb_image.data, w, h, bytes_per_line, QtGui.QImage.Format_RGB888)
        p = convert_to_Qt_format.scaled(self.image_disp_w_h, self.image_disp_w_h, Qt.KeepAspectRatio)
        return QPixmap.fromImage(p)
    
    def updateOptimizer(self):
        slider_position = self.horizontal_slider.value() / 999.0
        self.optimizer.submit_feedback_data(slider_position)
        print('Next')

        # optmized_para = self.optimizer.get_maximizer()
        optmized_para = self.optimizer.calc_point_from_slider_position(slider_position)

        stm_freq = 3 + optmized_para[0] * 7     # STM_freq: 3~10Hz
        radius = 2 + optmized_para[1] * 3       # STM radius: 2~5mm
        freq = int(50 + optmized_para[2] * 150) # wave freq: 50~200Hz
        amp = optmized_para[3]
        print('f_STM:', stm_freq, '\tradius: ', radius, '\tf_wave: ', freq, '\tamp: ', amp)
        
        self.autd_thread.SLS_para_signal.emit(np.array([stm_freq, radius, freq, amp]))

        offset = -0.5 * amp + 1
        self.sinusoid_widget.setAmplitude(amp)
        self.sinusoid_widget.setOffset(offset)
        self.sinusoid_widget.setFrequency(freq)

        i = 0
        for vertical_slider in self.vertical_sliders:
            vertical_slider.setValue(int(optmized_para[i] * vertical_slider.maximum()))
            i += 1

    def updateValues(self):
        t = self.horizontal_slider.value() / 999.0
        optmized_para = self.optimizer.calc_point_from_slider_position(t)

        stm_freq = 3 + optmized_para[0] * 7     # STM_freq: 3~10Hz
        radius = 2 + optmized_para[1] * 3       # STM radius: 2~5mm
        freq = int(50 + optmized_para[2] * 150) # wave freq: 50~200Hz
        amp = optmized_para[3]
        print('f_STM:', stm_freq, '\tradius: ', radius, '\tf_wave: ', freq, '\tamp: ', amp)
        
        self.autd_thread.SLS_para_signal.emit(np.array([stm_freq, radius, freq, amp]))

        offset = -0.5 * amp + 1
        self.sinusoid_widget.setAmplitude(amp)
        self.sinusoid_widget.setOffset(offset)
        self.sinusoid_widget.setFrequency(freq)

        i = 0
        for vertical_slider in self.vertical_sliders:
            vertical_slider.setValue(int(optmized_para[i] * vertical_slider.maximum()))
            i += 1


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())