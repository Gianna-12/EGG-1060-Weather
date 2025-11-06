const int sensorPin = A2;

void setup() {
  Serial.begin(9600);
}

void loop() {
  int sensorValue = analogRead(sensorPin);

  if (sensorValue > 10) {   // if the sensor detects any moisture at all
    Serial.println("rain");
  } else {
    Serial.println("no rain");
  }

  delay(500);
}
