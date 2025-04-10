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
        self.auto_position = True
        self.motor_active = 0
        self.R_PWM_OFFSET = 18  # GPIO23 (физ. пин 16)
        self.L_PWM_OFFSET = 19  # GPIO25 (физ. пин 22)
        self.chip = gpiod.Chip("/dev/gpiochip0")
        self.location = LocationInfo(name = "Storozhevka", 
                                     region = "Russia",
                                     timezone = "Europe/Saratov",
                                     latitude = 51.652209,
                                     longitude = 45.818634)
        self.today = date.today()
        self.sun = sun(self.location.observer, date = self.today, tzinfo =  self.location.timezone)
        self.log.info("Day:%s" % str(self.today))
        self.log.info("Sunrise: %s" % str(self.sun['sunrise']))
        self.log.info("Sunset: %s" % str(self.sun['sunset']))
        self.day_length = int((self.sun['sunset'] - self.sun['sunrise']).total_seconds())
        self.log.info("Day seconds: %s" % self.day_length)
        self.lines = gpiod.request_lines(
                       "/dev/gpiochip0",
                       consumer = "pwm-control",
                       config = {self.L_PWM_OFFSET: gpiod.LineSettings(direction=gpiod.line.Direction.OUTPUT),
                                 self.R_PWM_OFFSET: gpiod.LineSettings(direction=gpiod.line.Direction.OUTPUT)})    
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
        
        self.loop.create_task(self.overload_monitoring())
        self.loop.create_task(self.position_controller())
        self.loop.create_task(self.position_monitoring())
        self.loop.create_task(self.position_sync())

    def write_register(self, reg, value):
        self.spi.xfer2([reg & 0x3F, value]) 
        
    def read_ads(self):
        a = round(self.c1.voltage, 2)
        b = round(self.c2.voltage, 2)
        return (a, b)
        
    async def read_ads_async(self):
        return await asyncio.to_thread(self.read_ads)
		
    def spi_transfer_sync(self, data):
        return self.spi.xfer2(data)
	
    async def spi_transfer_async(self, data):
        return await asyncio.to_thread(self.spi_transfer_sync, data)
		
		
    def spi_write_register(self, reg, value):
        self.spi.xfer2([reg & 0x3F, value])
      
    def is_in_target_position(self):
        if self.position > self.target_position + 0.3:
            return False
        if self.position < self.target_position - 0.3:
            return False
        return True

    async def position_controller(self):
       await asyncio.sleep(1)  
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
               print("day seconds", self.day_length )
               print("day seconds left ", int(self.day_length - c))
               if c > self.day_length:
                   self.target_position = 0
                   self.log.info("Auto position: night position")
               else:
                   step_offset = int(c / step)
                   position = -1 * self.position_limit + step_offset
                   self.log.info("Auto position: %s" % position)
                   if position > self.position_limit:
                       position = self.position_limit
                   if position < (-1 * self.position_limit):
                      position = -1 * self.position_limit
                   self.target_position = position
           await asyncio.sleep(90)
		   
    async def stop_motor(self):
        self.lines.set_value(self.R_PWM_OFFSET, gpiod.line.Value.INACTIVE)
        self.lines.set_value(self.L_PWM_OFFSET, gpiod.line.Value.INACTIVE)
        self.motor_active = 0
        await asyncio.sleep(1)
        
    async def position_sync(self):
       await asyncio.sleep(1)  
       while not self.shutdown:
           if not self.is_in_target_position():
               if self.position > self.target_position:
                   ch =  self.L_PWM_OFFSET
                   if self.motor_active and self.motor_active != 1:
                       await self.stop_motor()
                   self.motor_active = 1	   
               else:
                   ch = self.R_PWM_OFFSET
                   if self.motor_active and self.motor_active != 2:
                       await self.stop_motor()
                   self.motor_active = 2	
               if not self.overload:
                   self.lines.set_value(ch, gpiod.line.Value.ACTIVE)
               else:
                   print("overload")
                   await asyncio.sleep(20)
                   await self.stop_motor()
           else:
               await self.stop_motor()
           await asyncio.sleep(0.2)
       await self.stop_motor()
       self.lines.release()
       self.lines_released = True

    async def overload_monitoring(self):
        while not self.shutdown:
            data = await self.read_ads_async() 
            if not self.overload and max(data) > self.overload_value:
                self.overload = True
                self.log.error("Overload: "+str(max(data)))
            await asyncio.sleep(0.2)

    async def position_monitoring(self):
        while not self.shutdown:
           data = await  self.spi_transfer_async([0xC0 | 0x32] + [0x00]*6)
           data = data[1:]
           x = (data[1] << 8) | data[0]
           x = x - 65536 if x & 0x8000 else x
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
        if not self.lines_released:
            try:
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
