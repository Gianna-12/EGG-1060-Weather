const int sensorPin = A2; //water level detector connected here
const int sensorPin = A1; //photoresistor connected here

void setup() {
  Serial.begin(9600);
}

void loop() {
  // Photoresistor code
  int sensorValue = analogRead(sensorPin);
  Serial.println(sensorValue);
  delay(100);

 // Water level detector code
  int sensorValue = analogRead(sensorPin);

  if (sensorValue > 10) {   // Check for moisture
    Serial.println("rain");
  } else {
    Serial.println("no rain");
  }

  delay(500);
}
