import logging

from rdpy.core.ssl import ClientTLSContext
from rdpy.crypto.crypto import SecuritySettings, RC4CrypterProxy
from rdpy.enum.core import ParserMode
from rdpy.enum.rdp import RDPPlayerMessageType
from rdpy.enum.segmentation import SegmentationPDUType
from rdpy.enum.virtual_channel.virtual_channel import VirtualChannel
from rdpy.layer.gcc import GCCClientConnectionLayer
from rdpy.layer.mcs import MCSLayer, MCSClientConnectionLayer
from rdpy.layer.raw import RawLayer
from rdpy.layer.rdp.connection import RDPClientConnectionLayer
from rdpy.layer.rdp.data import RDPDataLayer
from rdpy.layer.rdp.fastpath import FastPathLayer
from rdpy.layer.rdp.security import TLSSecurityLayer, RDPSecurityLayer
from rdpy.layer.rdp.virtual_channel.clipboard import ClipboardLayer
from rdpy.layer.rdp.virtual_channel.virtual_channel import VirtualChannelLayer
from rdpy.layer.segmentation import SegmentationLayer
from rdpy.layer.tcp import TCPLayer
from rdpy.layer.tpkt import TPKTLayer
from rdpy.layer.x224 import X224Layer
from rdpy.mcs.channel import MCSChannelFactory, MCSClientChannel
from rdpy.mcs.client import MCSClientRouter
from rdpy.mcs.user import MCSUserObserver
from rdpy.mitm.observer import MITMSlowPathObserver, MITMFastPathObserver
from rdpy.mitm.virtual_channel.clipboard import MITMClientClipboardChannelObserver
from rdpy.mitm.virtual_channel.virtual_channel import MITMVirtualChannelObserver
from rdpy.parser.rdp.fastpath import createFastPathParser
from rdpy.parser.rdp.negotiation import RDPNegotiationResponseParser, RDPNegotiationRequestParser
from rdpy.pdu.gcc import GCCConferenceCreateResponsePDU
from rdpy.recording.observer import RecordingFastPathObserver
from rdpy.recording.recorder import Recorder, FileLayer, SocketLayer


class MITMClient(MCSChannelFactory, MCSUserObserver):
    def __init__(self, server, fileHandle, socket):
        """
        :type server: rdpy.mitm.server.MITMServer
        :type fileHandle: file
        :type socket: socket.socket
        """
        MCSChannelFactory.__init__(self)

        self.server = server
        self.channelMap = {}
        self.channelDefinitions = []
        self.channelObservers = {}
        self.clipboardObserver = None
        self.useTLS = False
        self.user = None
        self.fastPathObserver = None
        self.conferenceCreateResponse = None
        self.serverData = None
        self.crypter = RC4CrypterProxy()
        self.securitySettings = SecuritySettings(SecuritySettings.Mode.CLIENT)
        self.securitySettings.addObserver(self.crypter)
        self.log = logging.getLogger("mitm.client.%s" % server.getFriendlyName())

        self.tcp = TCPLayer()
        self.tcp.createObserver(onConnection=self.startConnection, onDisconnection=self.onDisconnection)

        self.segmentation = SegmentationLayer()
        self.segmentation.createObserver(onUnknownHeader=self.onUnknownTPKTHeader)

        self.tpkt = TPKTLayer()

        self.x224 = X224Layer()
        self.x224.createObserver(onConnectionConfirm=self.onConnectionConfirm, onDisconnectRequest=self.onDisconnectRequest)

        self.mcs = MCSLayer()
        self.router = MCSClientRouter(self.mcs, self)
        self.mcs.addObserver(self.router)
        self.router.createObserver(onConnectResponse=self.onConnectResponse, onDisconnectProviderUltimatum=self.onDisconnectProviderUltimatum)

        self.mcsConnect = MCSClientConnectionLayer(self.mcs)

        self.gccConnect = GCCClientConnectionLayer("1")
        self.gccConnect.createObserver(onPDUReceived=self.onConferenceCreateResponse)

        self.rdpConnect = RDPClientConnectionLayer()
        self.rdpConnect.createObserver(onPDUReceived=self.onServerData)

        self.securityLayer = None
        self.io = RDPDataLayer()
        self.fastPathLayer = None

        self.tcp.setNext(self.segmentation)
        self.segmentation.attachLayer(SegmentationPDUType.TPKT, self.tpkt)
        self.tpkt.setNext(self.x224)
        self.x224.setNext(self.mcs)
        self.mcsConnect.setNext(self.gccConnect)
        self.gccConnect.setNext(self.rdpConnect)



        record_layers = [FileLayer(fileHandle)]

        if socket is not None:
            record_layers.append(SocketLayer(socket))

        self.recorder = Recorder(record_layers)

    def getProtocol(self):
        return self.tcp

    def startConnection(self):
        """
        Start the connection sequence to the target machine.
        """
        self.log.debug("TCP connected")
        negotiation = self.server.getNegotiationPDU()
        parser = RDPNegotiationRequestParser()
        self.x224.sendConnectionRequest(parser.write(negotiation))

    def onDisconnection(self, reason):
        self.log.debug("Connection closed")
        self.server.disconnect()

    def onDisconnectRequest(self, pdu):
        self.log.debug("X224 Disconnect Request received")
        self.disconnect()

    def disconnect(self):
        self.log.debug("Disconnecting")
        self.tcp.disconnect()

    def onUnknownTPKTHeader(self, header):
        self.log.error("Closing the connection because an unknown TPKT header was received. Header: 0x%02lx" % header)
        self.disconnect()

    def onConnectionConfirm(self, pdu):
        """
        Called when the X224 layer is connected.
        """
        self.log.debug("Connection Confirm received")

        parser = RDPNegotiationResponseParser()
        response = parser.parse(pdu.payload)

        if response.tlsSelected:
            self.tcp.startTLS(ClientTLSContext())
            self.useTLS = True

        self.server.onConnectionConfirm(pdu)

    def onConnectInitial(self, gccConferenceCreateRequest, clientData):
        """
        Called when a Connect Initial PDU is received.
        :param gccConferenceCreateRequest: the conference create request.
        :param clientData: the RDPClientDataPDU.
        """
        self.log.debug("Sending Connect Initial")

        if clientData.networkData:
            self.channelDefinitions = clientData.networkData.channelDefinitions

        self.gccConnect.conferenceName = gccConferenceCreateRequest.conferenceName
        self.rdpConnect.send(clientData)

    def onConnectResponse(self, pdu):
        """
        Called when an MCS Connect Response PDU is received.
        """
        if pdu.result != 0:
            self.log.error("MCS Connection Failed")
            self.server.onConnectResponse(pdu, None)
        else:
            self.log.debug("MCS Connection Successful")
            self.mcsConnect.recv(pdu)
            self.server.onConnectResponse(pdu, self.serverData)

    def onConferenceCreateResponse(self, pdu):
        """
        Called when a GCC Conference Create Response is received.
        :param pdu: the conference response PDU
        :type pdu: GCCConferenceCreateResponsePDU
        """
        self.conferenceCreateResponse = pdu

    def onServerData(self, serverData):
        """
        Called when the server data from the GCC Conference Create Response is received.
        """
        self.serverData = serverData
        self.securitySettings.generateClientRandom()
        self.securitySettings.serverSecurityReceived(serverData.security)

        self.channelMap[self.serverData.network.mcsChannelID] = "I/O"

        for index in range(len(serverData.network.channels)):
            channelID = serverData.network.channels[index]
            self.channelMap[channelID] = self.channelDefinitions[index].name

    def onAttachUserRequest(self):
        self.user = self.router.createUser()
        self.user.addObserver(self)
        self.user.attach()

    def onAttachConfirmed(self, user):
        # MCS Attach User Confirm successful
        self.server.onAttachConfirmed(user)

    def onAttachRefused(self, user, result):
        # MCS Attach User Confirm failed
        self.server.onAttachRefused(user, result)

    def onChannelJoinRequest(self, pdu):
        self.mcs.send(pdu)

    def buildChannel(self, mcs, userID, channelID):
        channelName = self.channelMap.get(channelID, None)
        channelLog = channelName + " (%d)" % channelID if channelName else channelID
        self.log.debug("building channel {} for user {}".format(channelLog, userID))

        if channelName == "I/O":
            channel = self.buildIOChannel(mcs, userID, channelID)
        elif channelName == VirtualChannel.CLIPBOARD:
            channel = self.buildClipboardChannel(mcs, userID, channelID)
        else:
            channel = self.buildVirtualChannel(mcs, userID, channelID)

        self.server.onChannelJoinAccepted(userID, channelID)
        return channel

    def createSecurityLayer(self):
        encryptionMethod = self.serverData.security.encryptionMethod

        if self.useTLS:
            return TLSSecurityLayer()
        else:
            return RDPSecurityLayer.create(encryptionMethod, self.crypter)

    def buildVirtualChannel(self, mcs, userID, channelID):
        channel = MCSClientChannel(mcs, userID, channelID)
        securityLayer = self.createSecurityLayer()
        rawLayer = RawLayer()

        channel.setNext(securityLayer)
        securityLayer.setNext(rawLayer)

        observer = MITMVirtualChannelObserver(rawLayer)
        rawLayer.addObserver(observer)
        self.channelObservers[channelID] = observer

        return channel

    def buildClipboardChannel(self, mcs, userID, channelID):
        """
        :type mcs: MCSLayer
        :param userID: The mcs user that builds the channel
        :param channelID: The channel ID to use to communicate in that channel
        :return: MCSClientChannel that handles the Clipboard virtual channel traffic from the server to the MITM.
        """
        # Create all necessary layers
        channel = MCSClientChannel(mcs, userID, channelID)
        securityLayer = self.createSecurityLayer()
        virtualChannelLayer = VirtualChannelLayer()
        clipboardLayer = ClipboardLayer()

        # Link layers together in the good order: MCS --> Security --> VirtualChannel --> Clipboard
        channel.setNext(securityLayer)
        securityLayer.setNext(virtualChannelLayer)
        virtualChannelLayer.setNext(clipboardLayer)

        # Create and link the MITM Observer for the client side to the clipboard layer.
        self.clipboardObserver = MITMClientClipboardChannelObserver(clipboardLayer, self.recorder)
        clipboardLayer.addObserver(self.clipboardObserver)

        self.channelObservers[channelID] = self.clipboardObserver

        return channel

    def buildIOChannel(self, mcs, userID, channelID):
        encryptionMethod = self.serverData.security.encryptionMethod
        self.securityLayer = self.createSecurityLayer()
        self.securityLayer.createObserver(onLicensingDataReceived=self.onLicensingDataReceived)

        slowPathObserver = MITMSlowPathObserver(self.log, self.io)
        self.io.addObserver(slowPathObserver)
        self.channelObservers[channelID] = slowPathObserver

        fastPathParser = createFastPathParser(self.useTLS, encryptionMethod, self.crypter, ParserMode.CLIENT)
        self.fastPathLayer = FastPathLayer(fastPathParser)
        self.fastPathObserver = MITMFastPathObserver(self.log, self.fastPathLayer)
        self.fastPathLayer.addObserver(self.fastPathObserver)
        self.fastPathLayer.addObserver(RecordingFastPathObserver(self.recorder, RDPPlayerMessageType.OUTPUT))

        channel = MCSClientChannel(mcs, userID, channelID)
        channel.setNext(self.securityLayer)
        self.securityLayer.setNext(self.io)

        self.segmentation.attachLayer(SegmentationPDUType.FAST_PATH, self.fastPathLayer)

        if self.useTLS:
            self.securityLayer.securityHeaderExpected = True
        elif encryptionMethod != 0:
            self.log.debug("Sending Security Exchange")
            self.io.previous.sendSecurityExchange(self.securitySettings.encryptClientRandom())

        return channel

    def onChannelJoinRefused(self, user, result, channelID):
        self.server.onChannelJoinRefused(user, result, channelID)

    def onClientInfoPDUReceived(self, pdu):
        self.log.debug("Sending Client Info: {}".format(pdu))

        self.securityLayer.sendClientInfo(pdu)

    def onLicensingDataReceived(self, data):
        self.log.debug("Licensing data received")

        if self.useTLS:
            self.securityLayer.securityHeaderExpected = False

        self.server.onLicensingDataReceived(data)

    def onDisconnectProviderUltimatum(self, pdu):
        self.log.debug("Disconnect Provider Ultimatum received")
        self.server.sendDisconnectProviderUltimatum(pdu)

    def getChannelObserver(self, channelID):
        return self.channelObservers[channelID]

    def getFastPathObserver(self):
        return self.fastPathObserver
