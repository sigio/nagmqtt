#!/usr/bin/env python                                                                                                            
# -*- coding: utf-8 -*-

import paho.mqtt.client as paho   # pip install paho-mqtt
import logging
import signal
import sys
import time
from datetime import datetime
try:
    import json
except ImportError:
    import simplejson as json
import os
import socket
from ConfigParser import RawConfigParser, NoOptionError
import codecs

HAVE_TLS = True
try:
    import ssl
except ImportError:
    HAVE_TLS = False


__author__    = 'Mark Janssen <mark()sig-io.nl>'
__copyright__ = 'Copyright 2014 Sig-I/O Automatisering'
__license__   = """Eclipse Public License - v 1.0 (http://www.eclipse.org/legal/epl-v10.html)"""

# script name (without extension) used for config/logfile names
SCRIPTNAME = os.path.splitext(os.path.basename(__file__))[0]

CONFIGFILE = os.getenv(SCRIPTNAME.upper() + 'INI', SCRIPTNAME + '.ini')
LOGFILE    = os.getenv(SCRIPTNAME.upper() + 'LOG', SCRIPTNAME + '.log')

# lwt values - may make these configurable later?
LWTALIVE   = "1"
LWTDEAD    = "0"                                                                                                                 
class Config(RawConfigParser):

  def __init__(self, configuration_file):
    RawConfigParser.__init__(self)
    f = codecs.open(configuration_file, 'r', encoding='utf-8')
    self.readfp(f)
    f.close()

    ''' set defaults '''
    self.mqttserver               = 'mosquitto.space.revspace.nl'
    self.mqttport                 = 1883
    self.username                 = None
    self.password                 = None
    self.clientid                 = SCRIPTNAME
    self.lwt                      = 'clients/%s' % SCRIPTNAME
    self.skipretained             = False
    self.cleansession             = False
    self.protocol                 = 3

    self.logformat                = '%(asctime)-15s %(levelname)-5s [%(module)s] %(message)s'
    self.logfile                  = LOGFILE
    self.loglevel                 = 'DEBUG'

    self.directory                = '.'
    self.ca_certs                 = None
    self.tls_version              = None
    self.certfile                 = None
    self.keyfile                  = None
    self.tls_insecure             = False
    self.tls                      = False

    if self.ca_certs is not None:
      self.tls = True

    if self.tls_version is not None:
      if self.tls_version == 'tlsv1':
        self.tls_version = ssl.PROTOCOL_TLSv1                                                                            
      if self.tls_version == 'sslv3':
        self.tls_version = ssl.PROTOCOL_SSLv3

try:
  cf = Config(CONFIGFILE)
except Exception, e:
  print "Cannot open configuration at %s: %s: " % (CONFIGFILE, str(e) )
  sys.exit(2)

LOGFORMAT = cf.logformat

# init logging
logging.basicConfig(filename=LOGFILE, level=10, format=LOGFORMAT)
logging.info("Starting %s" % SCRIPTNAME )
logging.info("INFO MODE" );
logging.debug("DEBUG MODE" );

mqttc = paho.Client(cf.clientid, clean_session=cf.cleansession, protocol=cf.protocol)


# MQTT broker callbacks
def on_connect(mosq, userdata, result_code):
    """
    Handle connections (or failures) to the broker.
    This is called after the client has received a CONNACK message
    from the broker in response to calling connect().

    The result_code is one of;
    0: Success
    1: Refused - unacceptable protocol version
    2: Refused - identifier rejected
    3: Refused - server unavailable
    4: Refused - bad user name or password (MQTT v3.1 broker only)
    5: Refused - not authorised (MQTT v3.1 broker only)
    """
    if result_code == 0:
        logging.debug("Connected to MQTT broker, subscribing to topics...")

        subscribed = []
        for section in get_sections():
            topic = get_topic(section)
            qos = get_qos(section)

            if topic in subscribed:
                continue

            logging.debug("Subscribing to %s (qos=%d)" % (topic, qos))
            mqttc.subscribe(str(topic), qos)
            subscribed.append(topic)

        mqttc.publish(cf.lwt, LWTALIVE, qos=0, retain=True)

    elif result_code == 1:
        logging.info("Connection refused - unacceptable protocol version")
    elif result_code == 2:
        logging.info("Connection refused - identifier rejected")
    elif result_code == 3:
        logging.info("Connection refused - server unavailable")
    elif result_code == 4:
        logging.info("Connection refused - bad user name or password")
    elif result_code == 5:
        logging.info("Connection refused - not authorised")
    else:
        logging.warning("Connection failed - result code %d" % (result_code))

def on_disconnect(mosq, userdata, result_code):
    """
    Handle disconnections from the broker
    """
    if result_code == 0:
        logging.info("Clean disconnection from broker")
    else:
        send_failover("brokerdisconnected", "Broker connection lost. Will attempt to reconnect in 5s...")
        time.sleep(5)

def on_message(mosq, userdata, msg):
    """
    Message received from the broker
    """
    topic = msg.topic
    payload = str(msg.payload)
    logging.debug("Message received on %s: %s" % (topic, payload))

    if msg.retain == 1:
        if cf.skipretained:
            logging.debug("Skipping retained message on %s" % topic)
            return

    # Try to find matching settings for this topic
    for section in get_sections():
        # Get the topic for this section (usually the section name but optionally overridden)
        match_topic = get_topic(section)
        if paho.topic_matches_sub(match_topic, topic):
            logging.debug("Section [%s] matches message on %s. Processing..." % (section, topic))
            # Check for any message filters
            if is_filtered(section, topic, payload):
                logging.debug("Filter in section [%s] has skipped message on %s" % (section, topic))
                continue
            # Send the message to any targets specified
            send_to_targets(section, topic, payload)
# End of MQTT broker callbacks

def connect():
    try:
        os.chdir(cf.directory)
    except Exception, e:
        logging.error("Cannot chdir to %s: %s" % (cf.directory, str(e)))
        sys.exit(2)

    logging.debug("Attempting connection to MQTT broker %s:%d..." % (cf.mqttserver, int(cf.mqttport)))
    mqttc.on_connect = on_connect
    mqttc.on_message = on_message
    mqttc.on_disconnect = on_disconnect

    # check for authentication
    if cf.username:
        mqttc.username_pw_set(cf.username, cf.password)

    # set the lwt before connecting
    logging.debug("Setting LWT to %s..." % (cf.lwt))
    mqttc.will_set(cf.lwt, payload=LWTDEAD, qos=0, retain=True)

    # Delays will be: 3, 6, 12, 24, 30, 30, ...
    # mqttc.reconnect_delay_set(delay=3, delay_max=30, exponential_backoff=True)

    if cf.tls == True:
        mqttc.tls_set(cf.ca_certs, cf.certfile, cf.keyfile, tls_version=cf.tls_version, ciphers=None)

    if cf.tls_insecure:
        mqttc.tls_insecure_set(True)

    try:
        mqttc.connect(cf.mqttserver, int(cf.mqttport), 60)
    except Exception, e:
        logging.error("Cannot connect to MQTT broker at %s:%d: %s" % (cf.mqttserver, int(cf.mqttport), str(e)))
        sys.exit(2)

    while True:
        try:
            mqttc.loop_forever()
        except socket.error:
            logging.info("MQTT server disconnected; sleeping")
            time.sleep(5)
        except:
            # FIXME: add logging with trace
            raise



def cleanup(signum=None, frame=None):
    """
    Signal handler to ensure we disconnect cleanly
    in the event of a SIGTERM or SIGINT.
    """

    global exit_flag

    exit_flag = True

    for ptname in ptlist:
        logging.debug("Cancel %s timer" % ptname)
        ptlist[ptname].cancel()

    logging.debug("Disconnecting from MQTT broker...")
    mqttc.publish(cf.lwt, LWTDEAD, qos=0, retain=True)
    mqttc.loop_stop()
    mqttc.disconnect()

    logging.info("Waiting for queue to drain")
    q_in.join()

    logging.debug("Exiting on signal %d", signum)
    sys.exit(signum)

if __name__ == '__main__':

    # use the signal module to handle signals
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    # connect to broker and start listening
    connect()                                                                                                                    


