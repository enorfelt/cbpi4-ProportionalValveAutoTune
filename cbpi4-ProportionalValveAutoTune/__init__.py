# -*- coding: utf-8 -*-
import time
import math
import board
import busio
import adafruit_mcp4725
import logging
from unittest.mock import MagicMock, patch
from collections import deque
from collections import namedtuple
import asyncio
import random
from time import localtime, strftime
from cbpi.api import *
from cbpi.api.dataclasses import NotificationType, NotificationAction

@parameters([
    Property.Sensor(
        "VolumeSensor",
        description="Select Volume Sensor that you want to use to be able to auto hold volume"),
    Property.Actor(label="PumpActor",
                   description="If selected starts the pump actor when auto hold is enabled"),
    Property.Number(label="Output_Step", configurable=True, default_value=100,
                    description="Default: 100. Sets the output when stepping up/down."),
    Property.Number(label="Max_Output", configurable=True, default_value=100,
                    description="Default: 100. Sets the max power output."),
    Property.Number(label="lockback_seconds", configurable=True, default_value=30,
                    description="Default: 30. How far back to look for min/max temps.")
])
class PropValveAutoTune(CBPiActor):
    def __init__(self, cbpi, id, props):
        super().__init__(cbpi, id, props)
        self._logger = logging.getLogger(type(self).__name__)
        self.target = None
        self.finished = False
        self.open = 0
        self.i2c = busio.I2C(board.SCL, board.SDA)
        self.dac = adafruit_mcp4725.MCP4725(self.i2c)
        self.volume_sensor = self.props.get("VolumeSensor", None)
        self.pump_actor = self.props.get("PumpActor", None)

    @action("Set Target Volume", parameters=[
        Property.Number(label="Target", configurable=True, description="Target Volume for auto hold")
    ])
    async def settarget(self, Target=0):
        self.target = float(Target)
        if self.target < 0:
            self.target = 0

    async def on_start(self):
        self.state = False        

    async def on(self, power=0):
        self.state = True

    async def auto_off(self):
        self.finished=True
        self.state = False

    async def off(self):
        self.state = False
        self.finished = False
        if self.props.get("PumpActor", None) is not None:
            await self.cbpi.actor.off(self.props.get("PumpActor"))

    def get_state(self):
        return self.state

    async def run(self):
        while self.running == True:
            if self.state == True:
                self.finished = False
                setpoint = self.target
                current_value = self.cbpi.sensor.get_sensor_value(self.volume_sensor).get("value")

                if setpoint is None:
                    self.cbpi.notify('PID AutoTune', 'You have not defined a target volume. System will set target to {} L and start AutoTune'.format(
                        15), NotificationType.WARNING)
                    setpoint = 15
                    await self.settarget(setpoint)

                if setpoint < current_value:
                    self.cbpi.notify(
                        'PID AutoTune', 'Your target volume is above the current volume. Choose a higher setpoint or wait until volume is below target volume and restart AutoTune', NotificationType.ERROR)
                    # await self.actor_off(self.heater)
                    await self.off()

                if self.props.get("PumpActor", None) is not None:
                    await self.cbpi.actor.on(self.props.get("PumpActor"), 100)

                self.cbpi.notify(
                    'PID AutoTune', 'AutoTune In Progress. Do not turn off Auto mode until AutoTuning is complete', NotificationType.INFO)

                sampleTime = 2
                wait_time = 2
                outstep = float(self.props.get("Output_Step", 100))
                outmax = float(self.props.get("Max_Output", 100))
                lookbackSec = float(self.props.get("lockback_seconds", 20))
                open_percent_old = 0
                try:
                    atune = AutoTuner(setpoint, outstep, sampleTime,
                                    lookbackSec, 0, outmax)
                except Exception as e:
                    self.cbpi.notify('PID autoTune', 'AutoTune Error: {}'.format(
                        str(e)), NotificationType.ERROR)
                    atune.log(str(e))
                    await self.auto_off()
                atune.log("AutoTune will now begin")

                try:
                    await self.set_open(open_percent_old)
                    while not atune.run(self.cbpi.sensor.get_sensor_value(self.volume_sensor).get("value")):
                        open_percent = atune.output
                        if open_percent != open_percent_old:
                            await self.set_open(open_percent)
                            open_percent_old = open_percent
                        await asyncio.sleep(sampleTime)

                    await self.auto_off()

                    if atune.state == atune.STATE_SUCCEEDED:
                        atune.log("AutoTune has succeeded")
                        for rule in atune.tuningRules:
                            params = atune.getPIDParameters(rule)
                            atune.log('rule: {0}'.format(rule))
                            atune.log('P: {0}'.format(params.Kp))
                            atune.log('I: {0}'.format(params.Ki))
                            atune.log('D: {0}'.format(params.Kd))
                            if rule == "brewing":
                                self.cbpi.notify('AutoTune has succeeded', "P Value: %.8f | I Value: %.8f | D Value: %.8f" % (
                                    params.Kp, params.Ki, params.Kd), action=[NotificationAction("OK")])
                    elif atune.state == atune.STATE_FAILED:
                        atune.log("AutoTune has failed")
                        self.cbpi.notify('PID AutoTune Error', "PID AutoTune has failed", action=[
                                        NotificationAction("OK")])

                except asyncio.CancelledError as e:
                    pass
                except Exception as e:
                    self._logger.error("PIDAutoTune Error {}".format(e))
                    # await self.actor_off(self.heater)
                    await self.stop()
                    pass
                finally:
                    # await self.actor_off(self.heater)
                    await self.stop()
                    pass
            else:
                await asyncio.sleep(1)
    
    async def set_open(self, Open):
        self._logger.info("set_open %s" % Open)
        self.open = Open
        self.dac.normalized_value = float(self.open / 100)
        await self.cbpi.actor.actor_update(self.id, Open)

    async def set_power(self, power):
        pass


class AutoTuner(object):
    PIDParams = namedtuple('PIDParams', ['Kp', 'Ki', 'Kd'])
    PEAK_AMPLITUDE_TOLERANCE = 0.8
    STATE_OFF = 'off'
    STATE_RELAY_STEP_UP = 'relay step up'
    STATE_RELAY_STEP_DOWN = 'relay step down'
    STATE_SUCCEEDED = 'succeeded'
    STATE_FAILED = 'failed'

    _tuning_rules = {
        # rule: [Kp_divisor, Ki_divisor, Kd_divisor]
        "ziegler-nichols": [34, 40, 160],
        "tyreus-luyben": [44,  9, 126],
        "ciancone-marlin": [66, 88, 162],
        "pessen-integral": [28, 50, 133],
        "some-overshoot": [60, 40,  60],
        "no-overshoot": [100, 40,  60],
        "brewing": [2.5, 3, 3600]
    }

    def __init__(self, setpoint, outputstep=10, sampleTimeSec=5, lookbackSec=60, outputMin=float('-inf'), outputMax=float('inf'), noiseband=0.5, getTimeMs=None):
        if setpoint is None:
            raise ValueError('Kettle setpoint must be specified')
        if outputstep < 1:
            raise ValueError('Output step % must be greater or equal to 1')
        if sampleTimeSec < 1:
            raise ValueError('Sample Time Seconds must be greater or equal to 1')
        if lookbackSec < sampleTimeSec:
            raise ValueError(
                'Lookback Seconds must be greater or equal to Sample Time Seconds (5)')
        if outputMin >= outputMax:
            raise ValueError('Min Output % must be less than Max Output %')

        self._inputs = deque(maxlen=round(lookbackSec / sampleTimeSec))
        self._sampleTime = sampleTimeSec * 1000
        self._setpoint = setpoint
        self._outputstep = outputstep
        self._noiseband = noiseband
        self._outputMin = outputMin
        self._outputMax = outputMax

        self._state = AutoTuner.STATE_OFF
        self._peakTimestamps = deque(maxlen=5)
        self._peaks = deque(maxlen=5)

        self._output = 0
        self._lastRunTimestamp = 0
        self._peakType = 0
        self._peakCount = 0
        self._initialOutput = 0
        self._inducedAmplitude = 0
        self._Ku = 0
        self._Pu = 0

        if getTimeMs is None:
            self._getTimeMs = self._currentTimeMs
        else:
            self._getTimeMs = getTimeMs

    @property
    def state(self):
        return self._state

    @property
    def output(self):
        return self._output

    @property
    def tuningRules(self):
        return self._tuning_rules.keys()

    def getPIDParameters(self, tuningRule='ziegler-nichols'):
        divisors = self._tuning_rules[tuningRule]
        kp = self._Ku / divisors[0]
        ki = kp / (self._Pu / divisors[1])
        kd = kp * (self._Pu / divisors[2])
        return AutoTuner.PIDParams(kp, ki, kd)

    def log(self, text):
        filename = "./logs/valve-autotune.log"
        formatted_time = strftime("%Y-%m-%d %H:%M:%S", localtime())

        with open(filename, "a") as file:
            file.write("%s,%s\n" % (formatted_time, text))

    def run(self, inputValue):
        now = self._getTimeMs()

        if (self._state == AutoTuner.STATE_OFF or self._state == AutoTuner.STATE_SUCCEEDED or self._state == AutoTuner.STATE_FAILED):
            self._initTuner(inputValue, now)
        elif (now - self._lastRunTimestamp) < self._sampleTime:
            return False

        self._lastRunTimestamp = now

        # check input and change relay state if necessary
        if (self._state == AutoTuner.STATE_RELAY_STEP_UP and inputValue > self._setpoint + self._noiseband):
            self._state = AutoTuner.STATE_RELAY_STEP_DOWN
            self.log('switched state: {0}'.format(self._state))
            self.log('input: {0}'.format(inputValue))
        elif (self._state == AutoTuner.STATE_RELAY_STEP_DOWN and inputValue < self._setpoint - self._noiseband):
            self._state = AutoTuner.STATE_RELAY_STEP_UP
            self.log('switched state: {0}'.format(self._state))
            self.log('input: {0}'.format(inputValue))

		# set output
        if (self._state == AutoTuner.STATE_RELAY_STEP_UP):
            self._output = self._initialOutput - self._outputstep
        elif self._state == AutoTuner.STATE_RELAY_STEP_DOWN:
            self._output = self._initialOutput + self._outputstep

		# respect output limits
        self._output = min(self._output, self._outputMax)
        self._output = max(self._output, self._outputMin)

		# identify peaks
        isMax = True
        isMin = True

        for val in self._inputs:
            isMax = isMax and (inputValue > val)
            isMin = isMin and (inputValue < val)

        self._inputs.append(inputValue)

		# we don't want to trust the maxes or mins until the input array is full
        if len(self._inputs) < self._inputs.maxlen:
            return False

		# increment peak count and record peak time for maxima and minima
        inflection = False

		# peak types:
		# -1: minimum
		# +1: maximum
        if isMax:
            if self._peakType == -1:
                inflection = True
            self._peakType = 1
        elif isMin:
            if self._peakType == 1:
                inflection = True
            self._peakType = -1

		# update peak times and values
        if inflection:
            self._peakCount += 1
            self._peaks.append(inputValue)
            self._peakTimestamps.append(now)
            self.log('found peak: {0}'.format(inputValue))
            self.log('peak count: {0}'.format(self._peakCount))

		# check for convergence of induced oscillation
		# convergence of amplitude assessed on last 4 peaks (1.5 cycles)
        self._inducedAmplitude = 0

        if inflection and (self._peakCount > 4):
            absMax = self._peaks[-2]
            absMin = self._peaks[-2]
            for i in range(0, len(self._peaks) - 2):
                self._inducedAmplitude += abs(self._peaks[i] - self._peaks[i+1])
                absMax = max(self._peaks[i], absMax)
                absMin = min(self._peaks[i], absMin)

            self._inducedAmplitude /= 6.0

			# check convergence criterion for amplitude of induced oscillation
            amplitudeDev = ((0.5 * (absMax - absMin) - self._inducedAmplitude)
							/ self._inducedAmplitude)

            self.log('amplitude: {0}'.format(self._inducedAmplitude))
            self.log('amplitude deviation: {0}'.format(amplitudeDev))

            if amplitudeDev < AutoTuner.PEAK_AMPLITUDE_TOLERANCE:
                self._state = AutoTuner.STATE_SUCCEEDED

		# if the autotune has not already converged
		# terminate after 10 cycles
        if self._peakCount >= 20:
            self._output = 0
            self._state = AutoTuner.STATE_FAILED
            return True

        if self._state == AutoTuner.STATE_SUCCEEDED:
            self._output = 0

			# calculate ultimate gain
            self._Ku = 4.0 * self._outputstep / (self._inducedAmplitude * math.pi)

			# calculate ultimate period in seconds
            period1 = self._peakTimestamps[3] - self._peakTimestamps[1]
            period2 = self._peakTimestamps[4] - self._peakTimestamps[2]
            self._Pu = 0.5 * (period1 + period2) / 1000.0
            return True

        return False

    def _currentTimeMs(self):
        return time.time() * 1000

    def _initTuner(self, inputValue, timestamp):
        self._peakType = 0
        self._peakCount = 0
        self._output = 0
        self._initialOutput = 0
        self._Ku = 0
        self._Pu = 0
        self._inputs.clear()
        self._peaks.clear()
        self._peakTimestamps.clear()
        self._peakTimestamps.append(timestamp)
        self._state = AutoTuner.STATE_RELAY_STEP_UP



def setup(cbpi):
    cbpi.plugin.register("PropValveAutoTune", PropValveAutoTune)
    pass
