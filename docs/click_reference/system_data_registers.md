

- System Data Registers (SD)
- Address System Nickname Description Read/Write
- SD1 _PLC_Error_Code Stores the current Error Code. If no Error, the value = 0. Read
- SD5 _Firmware_Version_L Stores the current Low portion of the Firmware Version.For example, if the Firmware Version is V1.10, then this register has a 10. Read
- SD6 _Firmware_Version_H Stores the current High portion of the Firmware Version.For example, if the Firmware Version is V1.10, then this register has a 1. Read
- SD7 _Sub_Firmware_Version_L Stores the current Low portion of the Sub Firmware Version.For example, if the Sub Firmware Version is V3.10, then this register has a 10. Read
- SD8 _Sub_Firmware_Version_H Stores the current High portion of the Sub Firmware Version.For example, if the Sub Firmware Version is V3.10, then this register has a 3. Read
- SD9 _Scan_Counter Increases the stored value on every Scan up to 32,767, then rolls back to 0 and starts Increasing again. Read
- SD10 _Current_Scan_Time Stores the current Scan Time value. Read
- SD11 _Minimum_Scan_Time Stores the Shortest scan time since the PLC was set to RUN mode. Read
- SD12 _Maximum_Scan_Time Stores the Longest scan time since the PLC was set to RUN mode. Read
- SD13 _Fixed_Scan_Time_Setup Stores the Fixed Scan Time Setup value (ms). Read
- SD14 _Interrupt_Scan_Time Stores the time spent executing interrupts during the previous scan (ms). Read
- SD19 _RTC_Year (4 digits) Stores the Real Time Clock'sCurrent Year as 4-Digits. Read
- SD20 _RTC_Year (2 digits) Stores the Real Time Clock's Current Year as 2-Digits. Read
- SD21 _RTC_Month(1-12) Stores the Real Time Clock'sCurrent Month. Read
- SD22 _RTC_Day(1-31) Stores the Real Time Clock's Current Day. Read
- SD23 _RTC_Day_of_the_Week Stores the Real Time Clock's Current Day of the Week.1 = Sunday2 = Monday3 = Tuesday4 = Wednesday5 = Thursday6 = Friday7 = Saturday Read
- SD24 _RTC_Hour(0-23) Stores the Real Time Clock's Current Hour. Read
- SD25 _RTC_Minute(0-59) Stores the Real Time Clock's Current Minute. Read
- SD26 _RTC_Second(0-59) Stores the Real Time Clock's Current Second. Read
- SD29 _RTC_New_Year(4 digits) Adjust the current Year (use with SC53). Read / Write
- SD31 _RTC_New_Month Adjust the current Month (use with SC53) (1-12). Read / Write
- SD32 _RTC_New_Day Adjust the current Day (use with SC53) (1-31). Read / Write
- SD34 _RTC_New_Hour Adjust the current Hour (use with SC55) (0-23). Read / Write
- SD35 _RTC_New_Minute Adjust the current Minute (use with SC55) (0-59). Read / Write
- SD36 _RTC_New_Second Adjust the current Second (use with SC55) (0-59). Read / Write
- SD40 _Port1_Received_Data_Len Stores the number of characters that were received through Com Port 1 in the ASCII format. Read / Write
- SD41 _Port1_No_Comm_Time Stores the time that has passed in Seconds since Com Port 1 received a message from the Network Master.Put 0 to Reset the Register. Read / Write
- SD42 _Port1_Rcv_Pkt_High_Cnt A counter for the number of high packet events on Ethernet Port 1. Read / Write
- SD50 _Port2_Received_Data_Len Stores the number of characters that were received through Com Port 2 in the ASCII format. Read / Write
- SD51 _Port2_No_Comm_Time Stores the time that has passed in Seconds since Com Port 2 received a message from the Network Master.Put 0 to Reset the Register. Read / Write
- SD60 _Port3_Received_Data_Len Stores the number of characters that were received through Com Port 3 in the ASCII format. Read / Write
- SD61 _Port3_No_Comm_Time Stores the time that has passed in Seconds since Com Port 3 received a message from the Network Master.Put 0 to Reset the Register. Read / Write
- SD62 _BT_Paired_Device_Count Stores the number of paired Bluetooth devices. Read
- SD63 _SD_Total_Memory_L Stores the SD card Total Memory Available Low Word (megabyte). Read
- SD64 _SD_Total_Memory_H Stores the SD card Total Memory Available High Word (megabyte). Read
- SD65 _SD_Free_Memory_L Stores the SD card Free Memory Available Low Word (megabyte). Read
- SD66 _SD_Free_Memory_H Stores the SD card Free Memory Available High Word (megabyte). Read
- SD67 _SD_Used_Memory_L Stores the SD card Used Memory Available Low Word (megabyte). Read
- SD68 _SD_Used_Memory_H Stores the SD card Used Memory Available High Word (megabyte). Read
- SD69 _SD_Error_Information Stores the SD error number (see [SD Card Error Codes](278.md)). Read
- SD70 _SD_Log_File_Number Stores the current number of Log Files on the SD. Read
- SD71 _CPU_AD_CH1_Value Analog AD CH1 Data Value Read
- SD72 _CPU_AD_CH2_Value Analog AD CH2 Data Value Read
- SD73 _CPU_AD_CH3_Value
_CPU_AD_CH1_Value Analog AD CH3 Data Value (4 Input Type)
Analog DA CH1 Data Value (Switching Type) Read
- SD74 _CPU_AD_CH4_Value
_CPU_AD_CH2_Value Analog AD CH4 Data Value (4 Input Type)
Analog DA CH2 Data Value (Switching Type) Read
- SD75 _CPU_DA_CH1_Value Analog DA CH1 Data Value (4 Input Type) Read
- SD76 _CPU_DA_CH2_Value Analog DA CH2 Data Value (4 Input Type) Read
- SD80 _Port1_IP_Address1 Stores the IP Octet #1. Read
- SD81 _Port1_IP_Address2 Stores the IP Octet #2. Read
- SD82 _Port1_IP_Address3 Stores the IP Octet #3. Read
- SD83 _Port1_IP_Address4 Stores the IP Octet #4. Read
- SD84 _Port1_Subnet_Mask1 Stores the Network Mask Octet #1. Read
- SD85 _Port1_Subnet_Mask2 Stores the Network Mask Octet #2. Read
- SD86 _Port1_Subnet_Mask3 Stores the Network Mask Octet #3. Read
- SD87 _Port1_Subnet_Mask4 Stores the Network Mask Octet #4. Read
- SD88 _Port1_Default_Gateway1 Stores the Gateway Address Octet #1. Read
- SD89 _Port1_Default_Gateway2 Stores the Gateway Address Octet #2. Read
- SD90 _Port1_Default_Gateway3 Stores the Gateway Address Octet #3. Read
- SD91 _Port1_Default_Gateway4 Stores the Gateway Address Octet #4. Read
- SD101 _EIP_ModuleStatus **0** = No Error.**1** = PLC Critical Error Detected, see SD1 for more information.**2** = High load detection, this occurs when many Interrupt programs delay the EtherNet/IP Adapter. Read
- SD102 _EIP_IdentityStatus **Bits 4:7 Combined****2** = At least one faulted Class 1 I/O Connection.**3** = No Class 1 I/O Connections established.**5** = Fault, Bit 10 or Bit 11 is Set.**6** = At least one Class 1 I/O Connection in run mode.**7** = At least one Class 1 I/O Connection established in idle mode.**Bit 8**, Either a connection Timed Out, or High load was detected.**Bit 11**, PLC Critical Error Detected, see SD1 for more information. Read
- SD103 _EIP_Con1_NodeStatus The value of the Connection Object Class (0x05) Attribute 1 "State" of Connection 1. A value of 3 means a Connection is established. Read
- SD104 _EIP_Con1_GeneralStatus This value indicates an error value when a Class 1 I/O connection was unsuccessful. This value was returned to the Scanner device. More detailed information can be found in [CLICK EtherNet/IP Error Codes](241.md). Read
- SD105 _EIP_Con1_ExtendedStatus This value indicates an extended error value when a Class 1 I/O connection was unsuccessful. This value was returned to the Scanner device. More detailed information can be found in [CLICK EtherNet/IP Error Codes](241.md). Read
- SD106 _EIP_Con1_LostCount A counter for the number of Class 1 Implicit lost packets for this connection. Read/Write
- SD107 _EIP_Con1_DisConCount A counter for the number of Class 1 Implicit disconnections. Read/Write
- SD108 _EIP_Con1_No_Comm_Time Stores the time that has passed in Seconds since Connection 1 received a message from the Client/Originator. Read/Write
- SD109 _EIP_Con2_NodeStatus The value of the Connection Object Class (0x05) Attribute 1 "State" of Connection 2. A value of 3 means a Connection is Established. Read
- SD110 _EIP_Con2_GeneralStatus This value indicates an error value when a Class 1 I/O connection was unsuccessful. This value was returned to the Scanner device. More detailed information can be found in [CLICK EtherNet/IP Error Codes](241.md). Read
- SD111 _EIP_Con2_ExtendedStatus This value indicates an extended error value when a Class 1 I/O connection was unsuccessful. This value was returned to the Scanner device. More detailed information can be found in [CLICK EtherNet/IP Error Codes](241.md). Read
- SD112 _EIP_Con2_LostCount A counter for the number of Class 1 Implicit lost packets for this connection. Read/Write
- SD113 _EIP_Con2_DisConCount A counter for the number of Class 1 Implicit disconnections. Read/Write
- SD114 _EIP_Con2_No_Comm_Time Stores the time that has passed in Seconds since Connection 2 received a message from the Client/Originator. Read/Write
- SD131 _Password_Failed_Count Stores the number of incorrect Logins since power up. Read
- SD132 _Port1_AL_Denied_No1_Cnt A counter for the number of rejected connections by newest device on the Allow List on Port1. Read
- SD133 _WLAN_AL_Denied_No1_Cnt A counter for the number of rejected connections by newest device on the Allow List on WLAN Port. Read
- SD134 _Port1_AL_Denied_Count A counter for the total number of rejected connections by the Allow List on Port1 since power up. This count is not reset when clearing the Allow List Denied Records. Read
- SD135 _WLAN_AL_Denied_Count A counter for the total number of rejected connections by the Allow List on WLAN Port since power up. This count is not reset when clearing the Allow List Denied Records. Read
- SD140 _S0_P1_Received_Data_Len Stores the number of characters that were received through DCM0 (Slot 0) Com Port 1 in the ASCII format. Read / Write
- SD141 _S0_P1_No_Comm_Time Stores the time that has passed in Seconds since DCM0 (Slot 0) Com Port 1 received a message from the Network Master.Put 0 to Reset the Register. Read / Write
- SD142 _S0_P2_Received_Data_Len Stores the number of characters that were received through DCM0 (Slot 0) Com Port 2 in the ASCII format. Read / Write
- SD143 _S0_P2_No_Comm_Time Stores the time that has passed in Seconds since DCM0 (Slot 0) Com Port 2 received a message from the Network Master.Put 0 to Reset the Register. Read / Write
- SD144 _S1_P1_Received_Data_Len Stores the number of characters that were received through DCM1 (Slot 1) Com Port 1 in the ASCII format. Read / Write
- SD145 _S1_P1_No_Comm_Time Stores the time that has passed in Seconds since DCM1 (Slot 1) Com Port 1 received a message from the Network Master.Put 0 to Reset the Register. Read / Write
- SD146 _S1_P2_Received_Data_Len Stores the number of characters that were received through DCM1 (Slot 1) Com Port 2 in the ASCII format. Read / Write
- SD147 _S1_P2_No_Comm_Time Stores the time that has passed in Seconds since DCM1 (Slot 1) Com Port 2 received a message from the Network Master.Put 0 to Reset the Register. Read / Write
- SD150 Remote PLC Connect Count Shows the number of connections using the Remote PLC ports. Read
- SD188 _Port1_MAC_Address1 Stores the MAC Octet #1. Read
- SD189 _Port1_MAC_Address2 Stores the MAC Octet #2. Read
- SD190 _Port1_MAC_Address3 Stores the MAC Octet #3. Read
- SD191 _Port1_MAC_Address4 Stores the MAC Octet #4. Read
- SD192 _Port1_MAC_Address5 Stores the MAC Octet #5. Read
- SD193 _Port1_MAC_Address6 Stores the MAC Octet #6. Read
- SD194 _WLAN_ST_MAC_Address1 Stores the MAC Octet #1 (C2 CPUs only). Read
- SD195 _WLAN_ST_MAC_Address2 Stores the MAC Octet #2 (C2 CPUs only). Read
- SD196 _WLAN_ST_MAC_Address3 Stores the MAC Octet #3 (C2 CPUs only). Read
- SD197 _WLAN_ST_MAC_Address4 Stores the MAC Octet #4 (C2 CPUs only). Read
- SD198 _WLAN_ST_MAC_Address5 Stores the MAC Octet #5 (C2 CPUs only). Read
- SD199 _WLAN_ST_MAC_Address6 Stores the MAC Octet #6 (C2 CPUs only). Read
- SD200 _WLAN_IP_Address1 Stores the IP Octet #1. Read
- SD201 _WLAN_IP_Address2 Stores the IP Octet #2. Read
- SD202 _WLAN_IP_Address3 Stores the IP Octet #3. Read
- SD203 _WLAN_IP_Address4 Stores the IP Octet #4. Read
- SD204 _WLAN_Subnet_Mask1 Stores the Network Mask Octet #1. Read
- SD205 _WLAN_Subnet_Mask2 Stores the Network Mask Octet #2. Read
- SD206 _WLAN_Subnet_Mask3 Stores the Network Mask Octet #3. Read
- SD207 _WLAN_Subnet_Mask4 Stores the Network Mask Octet #4. Read
- SD208 _WLAN_Default_Gateway1 Stores the Gateway Address Octet #1. Read
- SD209 _WLAN_Default_Gateway2 Stores the Gateway Address Octet #2. Read
- SD210 _WLAN_Default_Gateway3 Stores the Gateway Address Octet #3. Read
- SD211 _WLAN_Default_Gateway4 Stores the Gateway Address Octet #4. Read
- SD212 _WLAN_Signal_Strength Stores the signal strength to the access point (0-100%). Read
- SD213 _WLAN_Connection_Status Stores the current connection status:0 = No connection.1 = Connected.2 = Access Point not found.3 = Access Point found but password is wrong.4 = Connection lost.5 = Authentication failure.6 = Authentication failure (other). Read
- SD214 _WLAN_No_Com_Time Stores the time that has passed in Seconds since Com WLAN received a message from a Network Master.Put 0 to Reset the Register. Read/Write
- SD215 _WLAN_Rcv_Pkt_High_Cnt A counter for the number of high packet events on the WLAN Port. Read/Write
- SD216 _WLAN_Connected_Channel Stores the current WiFi channel to the access point. Read
- SD217 _WLAN_Country_Code Stores the current WLAN country code, which controls Wi-Fi radio settings to comply with the selected country's regulations. Read
- SD218 _WLAN_No_Connect_Status Stores the extended status codes when SD213 indicates a connection error. Please see the table below for additional information. Read
- SD301 _S0_ModuleId Module ID.2 = C2-0PCUA3 = C2-NRED0 = Any other module Read
- SD302 _S0_Major_Version Product Major Version. Read
- SD303 _S0_Minor_Version Product Minor Version. Read
- SD304 _S0_Hotfix_Version Product Hotfix Version. Read
- SD305 _S0_Release_Version Product Release Version. Read
- SD306 _S0_CPU_USAGE CPU usage of the intelligent module in slot 0. Read
- SD307 _S0_MEM_USAGE Memory usage of the intelligent module in slot 0. Read
- SD308 _S0_ERROR_CODE 1 = Application Disabled2 = Time issue detected

- The time is after January 19, 2038, and the time is set to January 1, 2000, 0:00.

3 = Time issue detected

- There was a request to set the time after January 19, 2038.

4 = Abnormal Termination of application.155 = The server certificate has expired.

- Time < Server certificate validity start date.

157 = The server certificate has expired.

- Time > Server certificate expiration date Read
- SD309 _S0_SD_TOTAL_MEM_L Along with SD310 displays the total memory capacity of the SD Card installing in the intelligent module in slot 0. Read
- SD310 _S0_SD_TOTAL_MEM_H Along with SD309 displays the total memory capacity of the SD Card installing in the intelligent module in slot 0. Read
- SD311 _S0_SD_FREE_MEM_L Along with SD312 displays the available memory capacity of the SD Card installing in the intelligent module in slot 0. Read
- SD312 _S0_SD_FREE_MEM_H Along with SD311 displays the available memory capacity of the SD Card installing in the intelligent module in slot 0. Read
- SD313 _S0_SD_USED_MEM_L Along with SD314 displays the used memory capacity of the SD Card installing in the intelligent module in slot 0. Read
- SD314 _S0_SD_USED_MEM_H Along with SD313 displays the used memory capacity of the SD Card installing in the intelligent module in slot 0. Read
- SD315 _S0_ETH_IP_Address1 Stores the IP Octet #1 of the Ethernet port in the intelligent module in slot 0. Read
- SD316 _S0_ETH_IP_Address2 Stores the IP Octet #2 of the Ethernet port in the intelligent module in slot 0. Read
- SD317 _S0_ETH_IP_Address3 Stores the IP Octet #3 of the Ethernet port in the intelligent module in slot 0. Read
- SD318 _S0_ETH_IP_Address4 Stores the IP Octet #4 of the Ethernet port in the intelligent module in slot 0. Read
- SD319 _S0_ETH_Subnet_Mask1 Stores the Network Mask Octet #1 of the Ethernet port in the intelligent module in slot 0. Read
- SD320 _S0_ETH_Subnet_Mask2 Stores the Network Mask Octet #2 of the Ethernet port in the intelligent module in slot 0. Read
- SD321 _S0_ETH_Subnet_Mask3 Stores the Network Mask Octet #3 of the Ethernet port in the intelligent module in slot 0. Read
- SD322 _S0_ETH_Subnet_Mask4 Stores the Network Mask Octet #4 of the Ethernet port in the intelligent module in slot 0. Read
- SD323 _S0_ETH_Default_Gateway1 Stores the Gateway Address Octet #1 of the Ethernet port in the intelligent module in slot 0. Read
- SD324 _S0_ETH_Default_Gateway2 Stores the Gateway Address Octet #2 of the Ethernet port in the intelligent module in slot 0. Read
- SD325 _S0_ETH_Default_Gateway3 Stores the Gateway Address Octet #3 of the Ethernet port in the intelligent module in slot 0. Read
- SD326 _S0_ETH_Default_Gateway4 Stores the Gateway Address Octet #4 of the Ethernet port in the intelligent module in slot 0. Read
- SD327 _S0_ETH_MAC_Address1 Stores the MAC Octet #1 of the Ethernet port in the intelligent module in slot 0. Read
- SD328 _S0_ETH_MAC_Address2 Stores the MAC Octet #2 of the Ethernet port in the intelligent module in slot 0. Read
- SD329 _S0_ETH_MAC_Address3 Stores the MAC Octet #3 of the Ethernet port in the intelligent module in slot 0. Read
- SD330 _S0_ETH_MAC_Address4 Stores the MAC Octet #4 of the Ethernet port in the intelligent module in slot 0. Read
- SD331 _S0_ETH_MAC_Address5 Stores the MAC Octet #5 of the Ethernet port in the intelligent module in slot 0. Read
- SD332 _S0_ETH_MAC_Address6 Stores the MAC Octet #6 of the Ethernet port in the intelligent module in slot 0. Read
- SD333 _S0_USB_IP_Address1 Stores the IP Octet #1 of the USB port in the intelligent module in slot 0. Read
- SD334 _S0_USB_IP_Address2 Stores the IP Octet #2 of the USB port in the intelligent module in slot 0. Read
- SD335 _S0_USB_IP_Address3 Stores the IP Octet #3 of the USB port in the intelligent module in slot 0. Read
- SD336 _S0_USB_IP_Address4 Stores the IP Octet #4 of the USB port in the intelligent module in slot 0. Read
- SD337 _S0_USB_Subnet_Mask1 Stores the Network Mask Octet #1 of the USB port in the intelligent module in slot 0. Read
- SD338 _S0_USB_Subnet_Mask2 Stores the Network Mask Octet #2 of the USB port in the intelligent module in slot 0. Read
- SD339 _S0_USB_Subnet_Mask3 Stores the Network Mask Octet #3 of the USB port in the intelligent module in slot 0. Read
- SD340 _S0_USB_Subnet_Mask4 Stores the Network Mask Octet #4 of the USB port in the intelligent module in slot 0. Read
- SD341 _S0_USB_Default_Gateway1 Stores the Gateway Address Octet #1 of the USB port in the intelligent module in slot 0. Read
- SD342 _S0_USB_Default_Gateway2 Stores the Gateway Address Octet #2 of the USB port in the intelligent module in slot 0. Read
- SD343 _S0_USB_Default_Gateway3 Stores the Gateway Address Octet #3 of the USB port in the intelligent module in slot 0. Read
- SD344 _S0_USB_Default_Gateway4 Stores the Gateway Address Octet #4 of the USB port in the intelligent module in slot 0. Read
- SD345 _S0_USB_MAC_Address1 Stores the MAC Octet #1 of the USB port in the intelligent module in slot 0. Read
- SD346 _S0_USB_MAC_Address2 Stores the MAC Octet #2 of the USB port in the intelligent module in slot 0. Read
- SD347 _S0_USB_MAC_Address3 Stores the MAC Octet #3 of the USB port in the intelligent module in slot 0. Read
- SD348 _S0_USB_MAC_Address4 Stores the MAC Octet #4 of the USB port in the intelligent module in slot 0. Read
- SD349 _S0_USB_MAC_Address5 Stores the MAC Octet #5 of the USB port in the intelligent module in slot 0. Read
- SD350 _S0_USB_MAC_Address6 Stores the MAC Octet #6 of the USB port in the intelligent module in slot 0. Read
- SD351 _S0_Application_USAGE Application CPU Usage.
C2-OPC UA Only. Read
- SD352 _S0_Session_Cnt Number of Client sessions in use.
C2-OPC UA Only. Read
- SD353 _S0_DataUpdateCycleTime Data acquisition cycle time [ms].
C2-OPC UA Only. Read
- SD401 _S1_ModuleId Module ID.2 = C2-0PCUA3 = C2-NRED0 = Any other module Read
- SD402 _S1_Major_Version Product Major Version. Read
- SD403 _S1_Minor_Version Product Minor Version. Read
- SD404 _S1_Hotfix_Version Product Hotfix Version. Read
- SD405 _S1_Release_Version Product Release Version. Read
- SD406 _S1_CPU_USAGE CPU usage of the intelligent module in slot 1. Read
- SD407 _S1_MEM_USAGE Memory usage of the intelligent module in slot 1. Read
- SD408 _S1_ERROR_CODE 1 = Application Disabled2 = Time issue detected

- The time is after January 19, 2038, and the time is set to January 1, 2000, 0:00.

3 = Time issue detected

- There was a request to set the time after January 19, 2038.

4 = Abnormal Termination of application.155 = The server certificate has expired.

- Time < Server certificate validity start date.

157 = The server certificate has expired.

- Time > Server certificate expiration date. Read
- SD409 _S1_SD_TOTAL_MEM_L Along with SD310 displays the total memory capacity of the SD Card installing in the intelligent module in slot 1. Read
- SD410 _S1_SD_TOTAL_MEM_H Along with SD309 displays the total memory capacity of the SD Card installing in the intelligent module in slot 1. Read
- SD411 _S1_SD_FREE_MEM_L Along with SD312 displays the available memory capacity of the SD Card installing in the intelligent module in slot 1. Read
- SD412 _S1_SD_FREE_MEM_H Along with SD311 displays the available memory capacity of the SD Card installing in the intelligent module in slot 1. Read
- SD413 _S1_SD_USED_MEM_L Along with SD314 displays the used memory capacity of the SD Card installing in the intelligent module in slot 1. Read
- SD414 _S1_SD_USED_MEM_H Along with SD313 displays the used memory capacity of the SD Card installing in the intelligent module in slot 1. Read
- SD415 _S1_ETH_IP_Address1 Stores the IP Octet #1 of the Ethernet port in the intelligent module in slot 1. Read
- SD416 _S1_ETH_IP_Address2 Stores the IP Octet #2 of the Ethernet port in the intelligent module in slot 1. Read
- SD417 _S1_ETH_IP_Address3 Stores the IP Octet #3 of the Ethernet port in the intelligent module in slot 1. Read
- SD418 _S1_ETH_IP_Address4 Stores the IP Octet #4 of the Ethernet port in the intelligent module in slot 1. Read
- SD419 _S1_ETH_Subnet_Mask1 Stores the Network Mask Octet #1 of the Ethernet port in the intelligent module in slot 1. Read
- SD420 _S1_ETH_Subnet_Mask2 Stores the Network Mask Octet #2 of the Ethernet port in the intelligent module in slot 1. Read
- SD421 _S1_ETH_Subnet_Mask3 Stores the Network Mask Octet #3 of the Ethernet port in the intelligent module in slot 1. Read
- SD422 _S1_ETH_Subnet_Mask4 Stores the Network Mask Octet #4 of the Ethernet port in the intelligent module in slot 1. Read
- SD423 _S1_ETH_Default_Gateway1 Stores the Gateway Address Octet #1 of the Ethernet port in the intelligent module in slot 1. Read
- SD424 _S1_ETH_Default_Gateway2 Stores the Gateway Address Octet #2 of the Ethernet port in the intelligent module in slot 1. Read
- SD425 _S1_ETH_Default_Gateway3 Stores the Gateway Address Octet #3 of the Ethernet port in the intelligent module in slot 1. Read
- SD426 _S1_ETH_Default_Gateway4 Stores the Gateway Address Octet #4 of the Ethernet port in the intelligent module in slot 1. Read
- SD427 _S1_ETH_MAC_Address1 Stores the MAC Octet #1 of the Ethernet port in the intelligent module in slot 1. Read
- SD428 _S1_ETH_MAC_Address2 Stores the MAC Octet #2 of the Ethernet port in the intelligent module in slot 1. Read
- SD429 _S1_ETH_MAC_Address3 Stores the MAC Octet #3 of the Ethernet port in the intelligent module in slot 1. Read
- SD430 _S1_ETH_MAC_Address4 Stores the MAC Octet #4 of the Ethernet port in the intelligent module in slot 1. Read
- SD431 _S1_ETH_MAC_Address5 Stores the MAC Octet #5 of the Ethernet port in the intelligent module in slot 1. Read
- SD432 _S1_ETH_MAC_Address6 Stores the MAC Octet #6 of the Ethernet port in the intelligent module in slot 1. Read
- SD433 _S1_USB_IP_Address1 Stores the IP Octet #1 of the USB port in the intelligent module in slot 1. Read
- SD434 _S1_USB_IP_Address2 Stores the IP Octet #2 of the USB port in the intelligent module in slot 1. Read
- SD435 _S1_USB_IP_Address3 Stores the IP Octet #3 of the USB port in the intelligent module in slot 1. Read
- SD436 _S1_USB_IP_Address4 Stores the IP Octet #4 of the USB port in the intelligent module in slot 1. Read
- SD437 _S1_USB_Subnet_Mask1 Stores the Network Mask Octet #1 of the USB port in the intelligent module in slot 1. Read
- SD438 _S1_USB_Subnet_Mask2 Stores the Network Mask Octet #2 of the USB port in the intelligent module in slot 1. Read
- SD439 _S1_USB_Subnet_Mask3 Stores the Network Mask Octet #3 of the USB port in the intelligent module in slot 1. Read
- SD440 _S1_USB_Subnet_Mask4 Stores the Network Mask Octet #4 of the USB port in the intelligent module in slot 1. Read
- SD441 _S1_USB_Default_Gateway1 Stores the Gateway Address Octet #1 of the USB port in the intelligent module in slot 1. Read
- SD442 _S1_USB_Default_Gateway2 Stores the Gateway Address Octet #2 of the USB port in the intelligent module in slot 1. Read
- SD443 _S1_USB_Default_Gateway3 Stores the Gateway Address Octet #3 of the USB port in the intelligent module in slot 1. Read
- SD444 _S1_USB_Default_Gateway4 Stores the Gateway Address Octet #4 of the USB port in the intelligent module in slot 1. Read
- SD445 _S1_USB_MAC_Address1 Stores the MAC Octet #1 of the USB port in the intelligent module in slot 1. Read
- SD446 _S1_USB_MAC_Address2 Stores the MAC Octet #2 of the USB port in the intelligent module in slot 1. Read
- SD447 _S1_USB_MAC_Address3 Stores the MAC Octet #3 of the USB port in the intelligent module in slot 1. Read
- SD448 _S1_USB_MAC_Address4 Stores the MAC Octet #4 of the USB port in the intelligent module in slot 1. Read
- SD449 _S1_USB_MAC_Address5 Stores the MAC Octet #5 of the USB port in the intelligent module in slot 1. Read
- SD450 _S1_USB_MAC_Address6 Stores the MAC Octet #6 of the USB port in the intelligent module in slot 1. Read
- SD451 _S1_Application_USAGE Application CPU Usage.
C2-OPC UA Only. Read
- SD452 _S1_Session_Cnt Number of Client sessions in use.
C2-OPC UA Only. Read
- SD453 _S1_DataUpdateCycleTime Data acquisition cycle time [ms].
C2-OPC UA Only. Read

| ![](Resources/Notepad2.gif) | Note: The Nicknames of all System Control Relays and System Data Registers start with an Underscore ( _ ) to indicate that they are System Nicknames. On the other hand, all user-defined Nicknames can not start with an Underscore. Therefore, System Nicknames can be easily identified by the Underscore at the beginning of the Nickname. |
| --- | --- |

- Relationship Between WLAN Connection Status Registers SD213 and SD218
- SD213 SD218
- 0 : WLAN_NO_CONNECTION 0 : No error
- 1 : WLAN_CONNECTED
- 2 : WLAN_ERR_NO_AP_FOUND 201 : WIFI_REASON_NO_AP_FOUND
- 3 : WLAN_ERR_INVALID_PASSWORD 15 : WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT
- 4 : WLAN_CONNECTION_LOST 4 : WIFI_REASON_ASSOC_EXPIRE
- 5 : WLAN_CONNECTION_FAIL 2 : WIFI_REASON_AUTH_EXPIRE
- 6 : WLAN_CONNECTION_FAIL_OTHER 1 : WIFI_REASON_UNSPECIFIED
- 3 : WIFI_REASON_AUTH_LEAVE
- 5 : WIFI_REASON_ASSOC_TOOMANY
- 6 : WIFI_REASON_NOT_AUTHED
- 7 : WIFI_REASON_NOT_ASSOCED
- 8 : WIFI_REASON_ASSOC_LEAVE
- 9 : WIFI_REASON_ASSOC_NOT_AUTHED
- 10 : WIFI_REASON_DISASSOC_PWRCAP_BAD
- 11 : WIFI_REASON_DISASSOC_SUPCHAN_BAD
- 13 : WIFI_REASON_IE_INVALID
- 14 : WIFI_REASON_MIC_FAILURE
- 16 : WIFI_REASON_GROUP_KEY_UPDATE_TIMEOUT
- 17 : WIFI_REASON_IE_IN_4WAY_DIFFERS
- 18 : WIFI_REASON_GROUP_CIPHER_INVALID
- 19 : WIFI_REASON_PAIRWISE_CIPHER_INVALID
- 20 : WIFI_REASON_AKMP_INVALID
- 21 : WIFI_REASON_UNSUPP_RSN_IE_VERSION
- 22 : WIFI_REASON_INVALID_RSN_IE_CAP
- 23 : WIFI_REASON_802_1X_AUTH_FAILED
- 24 : WIFI_REASON_CIPHER_SUITE_REJECTED
- 200 : WIFI_REASON_BEACON_TIMEOUT
- 202 : WIFI_REASON_AUTH_FAIL
- 203 : WIFI_REASON_ASSOC_FAIL
- 204 : WIFI_REASON_HANDSHAKE_TIMEOUT
- 205 : WIFI_REASON_CONNECTION_FAIL


[![Related Topics Link Icon](../Skins/Default/Stylesheets/Images/transparent.gif)Related Topics](javascript:void(0);)
