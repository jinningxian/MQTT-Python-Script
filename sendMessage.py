import paho.mqtt.publish as publish
import time

#HOST = "127.0.0.1"
PORT = 1883
TOPIC = "TEST"
HOST = input("Enter Brokder IP Address >> ")
#PORT = int(input("Enter Port Number >> "))
#TOPIC = input("Topic to Subscribe >> ")
def on_connect(client, userdata, flags, rc):
    print("Connected with result code "+str(rc))
    client.subscribe("test")

def on_message(client, userdata, msg):
    print(msg.topic+" "+msg.payload.decode("utf-8"))

def on_send():
    topic = TOPIC
    while True:
        message = input("Enter your message >>> ")
        client_id = time.strftime('%Y%m%d%H%M%S',time.localtime(time.time()))
        publish.single(topic, message, qos = 1,hostname=HOST,port=PORT, client_id=client_id,auth = {'username':"admin", 'password':"123456"})
        '''
        try:
            publish.single(topic, message, qos = 1,hostname=HOST,port=PORT, client_id=client_id)
        except:
            HOST = input("Enter Brokder IP Address >> ")
            PORT = int(input("Enter Port Number >> "))
            topic = TOPIC
            
         #,auth = {'username':"admin", 'password':"123456"})
'''
if __name__ == '__main__':
    on_send()


    
