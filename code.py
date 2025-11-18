"""
CircuitPython data-collector for Raspberry Pi Pico W (or RP2040 boards running
CircuitPython).

Drop this file as "code.py" onto the CIRCUITPY drive. The board must be running
CircuitPython (recommended) so the board shows up as a USB mass-storage device
— this makes the generated CSV visible immediately when you plug the board in.

Behavior:
- Samples every SAMPLE_INTERVAL_SECONDS (default 120s to match training cadence)
- Reads sensors:
  - BME280 (I2C) -> temp_c, pressure_hpa, humidity_rh (requires adafruit_bme280
  	library in CIRCUITPY/lib; otherwise placeholders are used)
  - Photoresistor -> ADC pin GP26 -> light_adc (0-65535 native, scaled to 0-1023)
  - Water sensor -> ADC pin GP27 -> water_adc (0-65535 scaled to 0-1023)
  - Anemometer (SparkFun 2-pin) -> GPIO input GP15; counts rising edges during
  	the sample interval and converts to a wind_adc (0-1023) using a simple map

- Appends rows to "weather_log.csv" in the CIRCUITPY root with header
  temp_c,pressure_hpa,humidity_rh,light_adc,wind_adc,water_adc

Notes:
- If you want full BME280 accuracy, install the Adafruit_BME280 library into
  CIRCUITPY/lib.
- The anemometer conversion factor is approximate; calibrate to your anemometer
  spec if you need accurate wind speed.

Wiring (short): see README in this directory for a full breadboard layout.
"""

import time
import board
import digitalio
import analogio
import busio
import os
from adafruit_bme280 import basic as adafruit_bme280
import rtc
import adafruit_ntp

# --- ADDED: Imports for WiFi and Adafruit IO ---
import ssl
import wifi
import socketpool
import adafruit_requests
from adafruit_io.adafruit_io import IO_HTTP, AdafruitIO_RequestError
# -----------------------------------------------

SAMPLE_INTERVAL_SECONDS = 30 # Your 30s interval is safe for Adafruit IO
CSV_FILENAME = "/weather_log.csv" # root of CIRCUITPY

# Pins (CircuitPython board names match RP2040 pin labels)
I2C_SDA = board.GP4
I2C_SCL = board.GP5
LIGHT_ADC_PIN = board.GP26 # ADC0
WATER_ADC_PIN = board.GP27 # ADC1
ANEMOMETER_PIN = board.GP15

# initialize ADCs
light_adc = analogio.AnalogIn(LIGHT_ADC_PIN)
water_adc = analogio.AnalogIn(WATER_ADC_PIN)

# Initialize anemometer input
anemo = digitalio.DigitalInOut(ANEMOMETER_PIN)
anemo.direction = digitalio.Direction.INPUT
anemo.pull = digitalio.Pull.UP

# Try to import BME280 driver (Adafruit). If missing, use placeholders.
try:
	i2c = busio.I2C(I2C_SCL, I2C_SDA)
	bme = adafruit_bme280.Adafruit_BME280_I2C(i2c)
	bme_available = True
except Exception:
	bme = None
	bme_available = False


def read_bme():
	if bme_available:
		try:
			temp_c = bme.temperature
			pressure_hpa = bme.pressure
			humidity = bme.humidity
			return round(temp_c, 2), round(pressure_hpa, 2), round(humidity, 2)
		except Exception:
			return None
	return None


def adc_to_10bit(adc_value_16):
	# CircuitPython AnalogIn returns 16-bit value (0..65535)
	return int((adc_value_16 / 65535.0) * 1023.0)


def count_anemo_pulses(duration_s):
	# Simple polling pulse counter for duration_s seconds. Not interrupt-driven
	# but fine for slow cadence (2 min). Counts rising edges.
	start = time.monotonic()
	end = start + duration_s
	last_state = anemo.value
	count = 0
	# To reduce CPU usage, sleep small amounts; still catching pulses reliably
	while time.monotonic() < end:
		cur = anemo.value
		if (not last_state) and cur:
			# rising edge
			count += 1
		last_state = cur
		time.sleep(0.01) # 10 ms poll
	return count


def pulses_to_wind_adc(pulses, duration_s):
	# Placeholder mapping: pulses/sec -> mph factor -> 0-1023
	if duration_s <= 0:
		return 0
	pps = pulses / duration_s
	# Use an approximate factor: 1 pps ~ 2 mph (tune per your anemometer)
	wind_mph = pps * 1.492
	wind_mph_cap = min(wind_mph, 60.0)
	adc = int((wind_mph_cap / 60.0) * 1023)
	return adc, round(wind_mph, 2)



def ensure_csv_header():
    try:
        # Try to get file stats
        os.stat(CSV_FILENAME)
        # If that ^ succeeds, file exists.
        print("CSV exists; appending new rows")
    except OSError:
        # If we get an OSError, the file doesn't exist. Create it.
        try:
            with open(CSV_FILENAME, "w") as f:
                f.write("timestamp,temp_c,pressure_hpa,humidity_rh,light_adc,wind_mph,water_adc\n")
                f.flush()
                print("Created CSV and wrote header")
        except Exception as e:
            print(f"Error creating new CSV file: {e}")
    except Exception as e:
        # Catch any other weird errors
        print(f"Error checking file status: {e}")

"""
def ensure_csv_header():
	try:
		if not os.path.exists(CSV_FILENAME):
			with open(CSV_FILENAME, "w") as f:
				f.write("temp_c,pressure_hpa,humidity_rh,light_adc,wind_adc,water_adc\n")
				f.flush()
				print("Created CSV and wrote header")
		else:
			print("CSV exists; appending new rows")
	except Exception as e:
		print("Error while ensuring CSV header:", e)
"""


# --- ADDED: WiFi and Adafruit IO Setup Block ---
print("Starting data collector...")
print("BME available:", bme_available)

# Get credentials from settings.toml
try:
    print("Fetching credentials from settings.toml...")
    WIFI_SSID = os.getenv("CIRCUITPY_WIFI_SSID")
    WIFI_PASSWORD = os.getenv("CIRCUITPY_WIFI_PASSWORD")
    AIO_USERNAME = os.getenv("AIO_USERNAME")
    AIO_KEY = os.getenv("AIO_KEY")
    if not WIFI_SSID:
        raise ValueError("CIRCUITPY_WIFI_SSID not found in settings.toml")
    print("Credentials loaded.")
except Exception as e:
    print(f"Error reading settings.toml: {e}")
    raise

# Connect to WiFi
print(f"Connecting to {WIFI_SSID}...")
try:
    wifi.radio.connect(WIFI_SSID, WIFI_PASSWORD)
    print("Connected to WiFi!")
except Exception as e:
    print(f"Failed to connect to WiFi: {e}")
    raise

# Setup for Adafruit IO
pool = socketpool.SocketPool(wifi.radio)
context = ssl.create_default_context()
requests = adafruit_requests.Session(pool, context)
io = IO_HTTP(AIO_USERNAME, AIO_KEY, requests)
print("Connected to Adafruit IO")
#-----------------Added for Time From the internet--------------------

print("Fetching time from NTP...")
try:
    # Set the timezone from settings.toml (if it exists)
    tz_str = os.getenv("CIRCUITPY_TZ")
    if tz_str:
        print(f"Setting timezone to: {tz_str}")
        os.environ["TZ"] = tz_str
    
    # Fetch time
    ntp = adafruit_ntp.NTP(pool)
    # Set the Pico's internal Real Time Clock
    rtc.RTC().datetime = ntp.datetime 
    print("Time set successfully!")
except Exception as e:
    print(f"Failed to get NTP time: {e}")
# ---------------------------------------------

# Get (or create) your feeds
print("Checking Adafruit IO Feeds...")
try:
    feed_temp = io.get_feed("temperature")
    feed_pressure = io.get_feed("pressure")
    feed_humidity = io.get_feed("humidity")
    feed_light = io.get_feed("light")
    feed_wind = io.get_feed("wind")
    feed_water = io.get_feed("water")
    print("Feeds OK.")
except AdafruitIO_RequestError:
    # If they don't exist, create them
    print("Feeds not found, creating them...")
    feed_temp = io.create_feed("temperature")
    feed_pressure = io.create_feed("pressure")
    feed_humidity = io.create_feed("humidity")
    feed_light = io.create_feed("light")
    feed_wind = io.create_feed("wind")
    feed_water = io.create_feed("water")
    print("Feeds created.")
# ------------------------------------------

# Now, set up the CSV
ensure_csv_header()

print("\n--- Starting Main Loop ---")

while True:
	# Read BME (if available)
	bme_vals = read_bme()
	if bme_vals is None:
		temp_c = 0.0
		pressure_hpa = 0.0
		humidity_rh = 0.0
	else:
		temp_c, pressure_hpa, humidity_rh = bme_vals

	# Read ADCs
	light_raw = light_adc.value
	water_raw = water_adc.value
	light_10 = adc_to_10bit(light_raw)
	water_10 = adc_to_10bit(water_raw)

	# Count anemometer pulses for the sample interval
	# This function is your main "sleep" or delay
	print(f"Counting anemometer pulses for {SAMPLE_INTERVAL_SECONDS} seconds...")
	pulses = count_anemo_pulses(SAMPLE_INTERVAL_SECONDS)
	wind_adc, wind_mph = pulses_to_wind_adc(pulses, SAMPLE_INTERVAL_SECONDS)
	
	# --- ADDED: Get and format the current time ---
	now = time.localtime()
    # Format as "HH:MM:SS" (e.g., "16:06:30")
	time_str = f"{now.tm_hour:02}:{now.tm_min:02}:{now.tm_sec:02}"
    # ---------------------------------------------

	# --- MODIFIED: Add time_str to the row ---
	row = f"{time_str},{temp_c:.2f},{pressure_hpa:.2f},{humidity_rh:.2f},{light_10},{wind_mph},{water_10}"

	# Append to file
	try:
		with open(CSV_FILENAME, "a") as f:
			f.write(row + "\n")
			f.flush()
		print("Wrote row to CSV:", row)
	except Exception as e:
		print("Failed to write CSV row:", e)

	# --- ADDED: Broadcast all sensor data to Adafruit IO ---
	print("Sending data to Adafruit IO...")
	try:
		io.send_data(feed_temp["key"], temp_c)
		io.send_data(feed_pressure["key"], pressure_hpa)
		io.send_data(feed_humidity["key"], humidity_rh)
		io.send_data(feed_light["key"], light_10)
		io.send_data(feed_wind["key"], wind_mph) # Sending the 0-1023 value
		io.send_data(feed_water["key"], water_10)
		print("Data sent successfully!")
	except Exception as e:
		print(f"Failed to send to Adafruit IO: {e}")
		# If it fails, we'll just try again on the next loop
	# ----------------------------------------------------

	# Also print human-friendly details
	print(f"Temp {temp_c} C | Pressure {pressure_hpa} hPa | Hum {humidity_rh}% | Light {light_10} | Wind {wind_mph} mph ({wind_adc}) | Water {water_10}")

	# Short sleep to allow serial output to flush (we already waited the interval)
	print("--- Loop Complete, Repeating ---")
	time.sleep(1)
	
"""

CircuitPython data-collector for Raspberry Pi Pico W (or RP2040 boards running
CircuitPython).

Drop this file as "code.py" onto the CIRCUITPY drive. The board must be running
CircuitPython (recommended) so the board shows up as a USB mass-storage device
— this makes the generated CSV visible immediately when you plug the board in.

Behavior:
- Samples every SAMPLE_INTERVAL_SECONDS (default 120s to match training cadence)
- Reads sensors:
  - BME280 (I2C) -> temp_c, pressure_hpa, humidity_rh (requires adafruit_bme280
    library in CIRCUITPY/lib; otherwise placeholders are used)
  - Photoresistor -> ADC pin GP26 -> light_adc (0-65535 native, scaled to 0-1023)
  - Water sensor -> ADC pin GP27 -> water_adc (0-65535 scaled to 0-1023)
  - Anemometer (SparkFun 2-pin) -> GPIO input GP15; counts rising edges during
    the sample interval and converts to a wind_adc (0-1023) using a simple map

- Appends rows to "weather_log.csv" in the CIRCUITPY root with header
  temp_c,pressure_hpa,humidity_rh,light_adc,wind_adc,water_adc

Notes:
- If you want full BME280 accuracy, install the Adafruit_BME280 library into
  CIRCUITPY/lib.
- The anemometer conversion factor is approximate; calibrate to your anemometer
  spec if you need accurate wind speed.

Wiring (short): see README in this directory for a full breadboard layout.
"""
"""
import time
import board
import digitalio
import analogio
import busio
import os
from adafruit_bme280 import basic as adafruit_bme280

SAMPLE_INTERVAL_SECONDS = 30  # 2 minutes
CSV_FILENAME = "/weather_log.csv"  # root of CIRCUITPY

# Pins (CircuitPython board names match RP2040 pin labels)
I2C_SDA = board.GP4
I2C_SCL = board.GP5
LIGHT_ADC_PIN = board.GP26  # ADC0
WATER_ADC_PIN = board.GP27  # ADC1
ANEMOMETER_PIN = board.GP15

# initialize ADCs
light_adc = analogio.AnalogIn(LIGHT_ADC_PIN)
water_adc = analogio.AnalogIn(WATER_ADC_PIN)

# Initialize anemometer input
anemo = digitalio.DigitalInOut(ANEMOMETER_PIN)
anemo.direction = digitalio.Direction.INPUT
anemo.pull = digitalio.Pull.UP

# Try to import BME280 driver (Adafruit). If missing, use placeholders.
try:
    
    i2c = busio.I2C(I2C_SCL, I2C_SDA)
    bme = adafruit_bme280.Adafruit_BME280_I2C(i2c)
    bme_available = True
except Exception:
    bme = None
    bme_available = False


def read_bme():
    if bme_available:
        try:
            temp_c = bme.temperature
            pressure_hpa = bme.pressure
            humidity = bme.humidity
            return round(temp_c, 2), round(pressure_hpa, 2), round(humidity, 2)
        except Exception:
            return None
    return None


def adc_to_10bit(adc_value_16):
    # CircuitPython AnalogIn returns 16-bit value (0..65535)
    return int((adc_value_16 / 65535.0) * 1023.0)


def count_anemo_pulses(duration_s):
    # Simple polling pulse counter for duration_s seconds. Not interrupt-driven
    # but fine for slow cadence (2 min). Counts rising edges.
    start = time.monotonic()
    end = start + duration_s
    last_state = anemo.value
    count = 0
    # To reduce CPU usage, sleep small amounts; still catching pulses reliably
    while time.monotonic() < end:
        cur = anemo.value
        if (not last_state) and cur:
            # rising edge
            count += 1
        last_state = cur
        time.sleep(0.01)  # 10 ms poll
    return count


def pulses_to_wind_adc(pulses, duration_s):
    # Placeholder mapping: pulses/sec -> mph factor -> 0-1023
    if duration_s <= 0:
        return 0
    pps = pulses / duration_s
    # Use an approximate factor: 1 pps ~ 2 mph (tune per your anemometer)
    wind_mph = pps * 1.492
    wind_mph_cap = min(wind_mph, 60.0)
    adc = int((wind_mph_cap / 60.0) * 1023)
    return adc, round(wind_mph, 2)


def ensure_csv_header():
    try:
        if not os.path.exists(CSV_FILENAME):
            with open(CSV_FILENAME, "w") as f:
                f.write("temp_c,pressure_hpa,humidity_rh,light_adc,wind_adc,water_adc\n")
                f.flush()
                print("Created CSV and wrote header")
        else:
            print("CSV exists; appending new rows")
    except Exception as e:
        print("Error while ensuring CSV header:", e)


print("Starting data collector")
print("BME available:", bme_available)
ensure_csv_header()

while True:
    # Read BME (if available)
    bme_vals = read_bme()
    if bme_vals is None:
        temp_c = 0.0
        pressure_hpa = 0.0
        humidity_rh = 0.0
    else:
        temp_c, pressure_hpa, humidity_rh = bme_vals

    # Read ADCs
    light_raw = light_adc.value
    water_raw = water_adc.value
    light_10 = adc_to_10bit(light_raw)
    water_10 = adc_to_10bit(water_raw)

    # Count anemometer pulses for the sample interval
    print(f"Counting anemometer pulses for {SAMPLE_INTERVAL_SECONDS} seconds...")
    pulses = count_anemo_pulses(SAMPLE_INTERVAL_SECONDS)
    wind_adc, wind_mph = pulses_to_wind_adc(pulses, SAMPLE_INTERVAL_SECONDS)

    # Format CSV row (matching notebook expected columns)
    row = f"{temp_c:.2f},{pressure_hpa:.2f},{humidity_rh:.2f},{light_10},{wind_adc},{water_10}"

    # Append to file
    try:
        with open(CSV_FILENAME, "a") as f:
            f.write(row + "\n")
            f.flush()
        print("Wrote row:", row)
    except Exception as e:
        print("Failed to write row:", e)

    # Also print human-friendly details
    print(f"Temp {temp_c} C | Pressure {pressure_hpa} hPa | Hum {humidity_rh}% | Light {light_10} | Wind {wind_mph} mph ({wind_adc}) | Water {water_10}")

    # Short sleep to allow serial output to flush (we already waited the interval)
    time.sleep(1)
"""