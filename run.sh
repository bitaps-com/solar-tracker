#!/bin/sh
docker container stop solar-tracker
docker container rm   solar-tracker
parentdir="$(dirname "$(pwd)")"
docker run   \
		   --name solar-tracker \
		   --device /dev/spidev0.0 \
		   --device /dev/i2c-1 \
		   --device /dev/gpiomem \
		   -v $(pwd):/app/ \
		   --privileged \
		   -it solar-tracker
