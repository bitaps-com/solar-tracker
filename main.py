import logging
import asyncio
import colorlog
import traceback
import signal
import sys
import spidev
from collections import deque
import gpiod
import time
import board
import busio
import adafruit_ads1x15.ads1015 as ADS
from adafruit_ads1x15.analog_in import AnalogIn
from datetime import date
from astral import LocationInfo
from astral.sun import sun
from datetime import datetime

class App:
	
    def __init__(self, loop, logger):
        self.loop = loop
        self.log = logger
        self.position_limit = 62 
        self.tolerance = 0.3
        self.auto_position = True
        self.motor_active = 0
        self.motor_start_timestamp = 0
        self.motor_start_position = 0
        self.correction = -9
        self.error = False
        self.error_msg = ""
        self.R_PWM_OFFSET = 18
        self.L_PWM_OFFSET = 19
        self.GREEN_LED= 22        
        self.RED_LED= 23
        self.chip = gpiod.Chip("/dev/gpiochip0")
        self.location = LocationInfo(name = "Storozhevka", 
                                     region = "Russia",
                                     timezone = "Europe/Saratov",
                                     latitude = 51.652209,
                                     longitude = 45.818634)
        self.today = date.today()
        self.sun = sun(self.location.observer,
                       date = self.today,
                       tzinfo =  self.location.timezone)
        self.log.info("Day:%s" % str(self.today))
        self.log.info("Sunrise: %s" % str(self.sun['sunrise']))
        self.log.info("Sunset: %s" % str(self.sun['sunset']))
        self.day_length = int((self.sun['sunset'] - self.sun['sunrise']).total_seconds())
        self.log.info("Day seconds: %s" % self.day_length)
        self.lines = gpiod.request_lines(
                       "/dev/gpiochip0",
                       consumer = "pwm-control",
                       config = {self.L_PWM_OFFSET: gpiod.LineSettings(direction=gpiod.line.Direction.OUTPUT),
                                 self.R_PWM_OFFSET: gpiod.LineSettings(direction=gpiod.line.Direction.OUTPUT),
                                 self.RED_LED: gpiod.LineSettings(direction=gpiod.line.Direction.OUTPUT, active_low = True),
                                 self.GREEN_LED: gpiod.LineSettings(direction=gpiod.line.Direction.OUTPUT, active_low = True)})    
        self.lines_released = False

        self.overload_value = 4
        self.position = 0
        self.target_position = 0
        self.position_stack = deque(maxlen=40)
        self.shutdown = False
        self.force_shutdown = False
        self.spi = spidev.SpiDev()
        self.spi.open(0, 0)              
        self.spi.max_speed_hz = 500000   
        self.spi.mode = 3 
        self.write_register(0x2D, 0x08)  # POWER_CTL: Measurement mode
        self.write_register(0x31, 0x08)  # DATA_FORMAT: Full resolution, ±2g
        self.i2c = busio.I2C(board.SCL, board.SDA)
        self.ads = ADS.ADS1015(self.i2c)
        self.c1 = AnalogIn(self.ads, ADS.P0, ADS.P1)
        self.c2 = AnalogIn(self.ads, ADS.P2, ADS.P3)
        self.c1_load = 0
        self.c2_load = 0
        self.overload = None
        self.log.info("Solar tracker starting ...")
        signal.signal(signal.SIGINT, self.terminate)
        signal.signal(signal.SIGTERM, self.terminate)
        self.loop.create_task(self.start())
        
        
    async def start(self):
        self.loop.create_task(self.watchdog())
        self.loop.create_task(self.position_monitoring())
        self.loop.create_task(self.position_controller())
        await asyncio.sleep(2)
        self.loop.create_task(self.position_sync())

    def write_register(self, reg, value):
        self.spi.xfer2([reg & 0x3F, value]) 
        
    def read_ads(self):
        a = round(self.c1.voltage, 2)
        b = round(self.c2.voltage, 2)
        return (a, b)
        
    async def read_ads_async(self):
        return await asyncio.to_thread(self.read_ads)
		
    def spi_transfer(self, data):
        return self.spi.xfer2(data)
	
    async def spi_transfer_async(self, data):
        return await asyncio.to_thread(self.spi_transfer, data)
		
		
    def spi_write_register(self, reg, value):
        self.spi.xfer2([reg & 0x3F, value])
      
    def is_in_target_position(self):
        if self.position > self.target_position + self.tolerance:
            return False
        if self.position < self.target_position - self.tolerance:
            return False
        return True
    

    async def position_controller(self):
       while not self.shutdown:
           day = date.today()
           if day != self.today:
               self.today = day
               self.sun = sun(self.location.observer, date = self.today, tzinfo =  self.location.timezone)
               self.log.info("Day:%s" % str(self.today))
               self.log.info("Sunrise: %s" % str(self.sun['sunrise']))
               self.log.info("Sunset: %s" % str(self.sun['sunset']))
               self.day_length = int((self.sun['sunset'] - self.sun['sunrise']).total_seconds())
               self.log.info("Day seconds: %s" % self.day_length)

           if self.auto_position:
               step = self.day_length / (self.position_limit * 2)
               c = datetime.now().timestamp() - self.sun['sunrise'].timestamp()
               if c > self.day_length:
                   self.target_position = 0
                   self.log.info("Auto position: night position")
               else:
                   s = int(self.day_length - c)
                   step_offset = int(c / step)
                   position = -1 * self.position_limit + step_offset
                   self.log.info("Auto position: day %s degree %s h %s m left" % 
                                (position, s // 3600, (s - (3600 * (s // 3600))) // 60))
                   if position > self.position_limit:
                       position = self.position_limit
                   if position < (-1 * self.position_limit):
                      position = -1 * self.position_limit
                   self.target_position = position
           await asyncio.sleep(90)
		   
    async def stop_motor(self):
        self.lines.set_value(self.R_PWM_OFFSET, gpiod.line.Value.INACTIVE)
        self.lines.set_value(self.L_PWM_OFFSET, gpiod.line.Value.INACTIVE)
        if self.motor_active:
            d = abs(round(self.motor_start_position - self.position,2))
            self.log.debug("Motor stopped, stop %s, total %s" % (self.target_position, d))
        self.motor_active = 0
        self.motor_start_timestamp = 0
        await asyncio.sleep(1)

    async def start_motor(self, ch):
        if not self.motor_active:
            if not self.error:
                self.motor_active = 1 if ch == self.L_PWM_OFFSET else 2
                self.motor_start_timestamp = int(time.time())
                self.motor_start_position = self.position
                self.lines.set_value(ch, gpiod.line.Value.ACTIVE)
                self.log.debug("Motor started, start: %s target: %s" % 
                              (self.position, self.target_position))
            else:
                self.log.error("Motor start failed:" + self.error_msg)
                await self.stop_motor()
                await asyncio.sleep(60)
          

    async def position_sync(self):
       while not self.shutdown:
           if not self.is_in_target_position():
               if self.position > self.target_position:
                   ch =  self.L_PWM_OFFSET
                   if self.motor_active and self.motor_active != 1:
                       await self.stop_motor()   
               else:
                   ch = self.R_PWM_OFFSET
                   if self.motor_active and self.motor_active != 2:
                       await self.stop_motor()	
               await self.start_motor(ch)
           else:
               await self.stop_motor()
           await asyncio.sleep(0.2)
       await self.stop_motor()


    async def watchdog(self):
        motor_start = 0
        led_status = 0
        while not self.shutdown:
            # motor overload protection
            data = await self.read_ads_async()
            if not self.error and max(data) > self.overload_value:
                await self.stop_motor()
                self.error = True
                self.error_msg = "Motor overload: %s position: %s" % (max(data), self.position)
                self.log.error(self.error_msg)
            # adxl 
            if self.motor_active:
                if motor_start != self.motor_start_timestamp:
                    adxl_watchdog = 0
                if adxl_watchdog:
                    if int(time.time()) - adxl_watchdog > 10:
                        if  position_watchdog == self.position:
                            self.error = True
                            self.error_msg = "ADXL sensor failed"
                            self.log.error(self.error_msg)
                            await self.stop_motor()
                        adxl_watchdog = int(time.time())
                        position_watchdog = self.position 
                else:
                    motor_start = self.motor_start_timestamp
                    adxl_watchdog = int(time.time())
                    position_watchdog = self.position
            if self.error and led_status != 0:
                led_status = 0
                self.lines.set_value(self.GREEN_LED, gpiod.line.Value.INACTIVE)
                self.lines.set_value(self.RED_LED, gpiod.line.Value.INACTIVE)
            if not self.error and led_status == 0:
                led_status = 1
                self.lines.set_value(self.GREEN_LED, gpiod.line.Value.ACTIVE)
                self.lines.set_value(self.RED_LED, gpiod.line.Value.ACTIVE)
					
            await asyncio.sleep(0.1)

            
    async def position_monitoring(self):
        adxl_watchdog = 0
        position_watchdog = 0
        motor_start = 0

        while not self.shutdown:
           data = await  self.spi_transfer_async([0xC0 | 0x32] + [0x00]*6)
           data = data[1:]
           x = (data[1] << 8) | data[0]
           x = x - 65536 if x & 0x8000 else x 
           x = x + self.correction 
           self.position_stack.append(x)
           self.position = round((sum(self.position_stack)/len(self.position_stack))* 0.28, 1)

           await asyncio.sleep(0.1)


    def _exc(self, a, b, c):
        return

    def terminate(self, a, b):
        if not self.shutdown:
            self.shutdown = True
            self.loop.create_task(self.terminate_coroutine())
        else:
            if not self.force_shutdown:
                self.log.critical("Shutdown in progress please wait ... (or press CTRL + C to force shutdown)")
                self.force_shutdown = True
            else:
                self.log.critical("Force shutdown ...")
                if not self.lines_released:
                    try:
                        self.lines.set_value(self.R_PWM_OFFSET, gpiod.line.Value.INACTIVE)
                        self.lines.set_value(self.L_PWM_OFFSET, gpiod.line.Value.INACTIVE)
                        self.lines.release()
                        self.log.critical("Motor lines released")
                    except Exception as err:
                        self.log.critical("Motor release exception:" + str(err))
                sys.exit(0)

    async def terminate_coroutine(self):
        try:
            self.lines.set_value(self.GREEN_LED, gpiod.line.Value.INACTIVE)
            self.lines.set_value(self.RED_LED, gpiod.line.Value.INACTIVE)
            self.lines.set_value(self.L_PWM_OFFSET, gpiod.line.Value.INACTIVE)
            self.lines.set_value(self.L_PWM_OFFSET, gpiod.line.Value.INACTIVE)
            await asyncio.sleep(0.2)
            self.lines.release()
            self.log.error("Motor lines released")
        except Exception as err:
            self.log.critical("Motor release exception:" + str(err))
        sys.excepthook = self._exc
        self.log.error('Stop request received')
        self.loop.stop()
        self.log.info("Server has been stopped")
		



if __name__ == '__main__':
	log_level = logging.DEBUG
	logger = colorlog.getLogger("trx")
	logger.setLevel(log_level)
	ch = logging.StreamHandler()
	ch.setLevel(log_level)
	formatter = colorlog.ColoredFormatter('%(log_color)s%(asctime)s: %(message)s')
	ch.setFormatter(formatter)
	logger.addHandler(ch)

	loop = asyncio.get_event_loop()
	app = App(loop, logger)
	loop.run_forever()

	pending = asyncio.Task.all_tasks()
	for task in pending:
		task.cancel()
	if pending:
		loop.run_until_complete(asyncio.wait(pending))
	loop.close()
