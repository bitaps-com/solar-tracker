FROM python:3.11-slim
RUN apt-get update
RUN apt-get install -y i2c-tools 
RUN apt-get install -y python3-smbus python3-dev 
RUN apt-get install -y libffi-dev libatlas-base-dev libopenblas-dev
RUN apt-get install -y gcc build-essential python3-rpi.gpio
RUN apt-get install -y python3-wheel python3-pip
  
COPY requirements.txt .
RUN pip3 install wheel
RUN pip3 install --upgrade setuptools
RUN pip3 install  RPi.GPIO
RUN pip3 install --requirement requirements.txt
    
WORKDIR /app
COPY . /app
CMD ["python3", "main.py"]
