
import paho.mqtt.client as mqtt
import time

#HOST = "127.0.0.1"
PORT = 1883
TOPIC = "TEST"
HOST = input("Enter Brokder IP Address >> ")
#PORT = int(input("Enter Port Number >> "))
#TOPIC = input("Topic to Subscribe >> ")

def client_loop():
    topic = TOPIC
    client_id = time.strftime('%Y%m%d%H%M%S',time.localtime(time.time()))
    client = mqtt.Client(client_id)    
    client.username_pw_set("admin", "123456")  
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(HOST, PORT, 60)
    '''
    try:
        client.connect(HOST, PORT, 60)
    except:
        HOST = input("Enter Brokder IP Address >> ")
        PORT = int(input("Enter Port Number >> "))
        topic = TOPIC
        client_loop()
    '''
    client.loop_forever()

def on_connect(client, userdata, flags, rc):
    print("Connected with result code "+str(rc))
    client.subscribe(TOPIC)

def on_message(client, userdata, msg):
    print(msg.payload.decode("utf-8"))

if __name__ == '__main__':
    client_loop()
