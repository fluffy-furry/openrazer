#!/bin/bash

set -e

if [ "$1" = "-f" ]; then
    echo "Removing existing fake driver files..."
    rm $(dirname "$0")/../pylib/openrazer/_fake_driver/*.cfg
fi

drivers=$(sed -n 's/^obj-m[[:space:]]*:=[[:space:]]*//p' driver/Makefile | sed 's/\.o//g')

for driver in $drivers; do
    [ "$driver" = "razercore" ] && continue # razercore is currently broken
    $(dirname "$0")/generate_fake_driver.sh $driver
done
