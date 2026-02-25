

| System Control Relays | Topic: CL109<br>![](Resources/ClickLogo.gif) |
| --- | --- |


- System Control Relays (SC)
- Address System Nickname Description Read/Write
- SC1 _Always_ON Always ON. Read
- SC2 _1st_SCAN ON for the First Scan only. Read
- SC3 _SCAN_Clock Changes from OFF to ON or ON to OFF every Scan. Read
- SC4 _10ms_Clock Changes from OFF to ON and ON to OFF every 5ms. Read
- SC5 _100ms_Clock Changes from OFF to ON and ON to OFF every 50ms. Read
- SC6 _500ms_Clock Changes from OFF to ON and ON to OFF every 250ms. Read
- SC7 _1sec_Clock Changes from OFF to ON and ON to OFF every 500ms. Read
- SC8 _1min_Clock Changes from OFF to ON and ON to OFF every 30 seconds. Read
- SC9 _1hour_Clock Changes from OFF to ON and ON to OFF every 30 minutes. Read
- SC10 _Mode_Switch ON when PLC Mode Switch is in RUN position. Read
- SC11 _PLC_Mode ON when the PLC is in RUN mode. Read
- SC19 _PLC_Error ON when an Error occurs. Read
- SC20 _I/O_BUS_Error ON when an Error occurs. Read
- SC21 _System_Config_Error ON when an Error occurs. Read
- SC22 _I/O_Module_Error ON when an Error occurs. Read
- SC23 _Flash_Memory Error ON when an Error occurs. Read
- SC24 _Project_File_Error ON when an Error occurs. Read
- SC25 _Project_File_Ver_Error ON when an Error occurs. Read
- SC26 _Watchdog_Timer_ Error ON when an Error occurs. Read
- SC27 _Lost_SDRAM_Data ON when an Error occurs. Read
- SC28 _Battery_Low_Voltage ON when the Battery Voltage is Low (<2.5V). When this bit is ON, replace the Battery as soon as possible. Read
- SC29 _Battery_Replacement ON when the anticipated replacement date has passed. When this bit is ON, replace the Battery as soon as possible. Set the new Battery installation date and Battery replacement date in the Battery Backup Setup window (Drop-down Menu: Setup > Battery Backup Setup). Read
- SC30 _Run Edit Project Error The RUN Time Edit program download failed. The program download was not completed. The PLC will continue in Run with the previous program. Read
- SC31 _Sub_CPU_FW_Ver_Error Firmware in Sub CPU does not match the Main CPU. Update Firmware again. Read
- SC32 _C2INT_FW_Ver_Error The intelligent module contains a firmware version that is incompatible the main processor. Connect the CLICK software to the CLICK PLC and update the firmware of the CPU module. Read
- SC33 _CPLD Version Error CPLD Version Error. Read
- SC40 _Division_Error ON when a value is being Divided by "0". Read
- SC43 _Out_of_Range ON when there is a Data Overflow, Underflow, and Data Convert Error. Read
- SC44 _Address_Error ON when an Address is not valid. Read
- SC46 _Math_Operation_Error Data Registers used in the Math formula have Invalid values. SC50 is turned to ON, the PLC is put in Stop Mode. Read
- SC50 _PLC_Mode_Change_to_STOP When in Run mode it sets mode to Stop. Read/Write
- SC51 _Watchdog_Timer_Reset When SC51 is turned to ON, Watchdog Timer resets to "0". Read/Write
- SC53 _RTC_Date_Change Set this bit to apply [SD29, SD31, SD32.](system_data_registers.md) Read/Write
- SC54 _RTC_Date_Change_Error Status after Data Change 1=Error. Read
- SC55 _RTC_Time_Change Set this bit to apply [SD34, SD35, SD36.](system_data_registers.md) Read/Write
- SC56 _RTC_Time_Change_Error Status after Time Change 1=Error. Read
- SC60 _BT_Disable_Pairing Bluetooth Pairing can be Disabled by setting this bit ON. Read/Write
- SC61 _BT_Activate_Pairing Bluetooth Pairing can be activated by setting this bit ON. It will also be set ON when activated by the Pairing button. Read/Write
- SC62 _BT_Paired_Devices Indicates that at least one Bluetooth device has been paired. This bit also indicates the Bluetooth blink LED state. Read
- SC63 _BT_Pairing_SW_State Status of the Bluetooth button state 1=Pressed. Read
- SC64 _RemotPLC_Enabled The remote PLC feature is enabled Read
- SC65 _SD_Eject Set this bit ON to ask the system prepare to safely remove the SD card. The bit will turn OFF when the SD card is ready to be removed. Read/Write
- SC66 _SD_Delete_All Set this bit ON to ask the system to delete all log files on the SD card. The bit will turn OFF when the action is completed. Read/Write
- SC67 _SD_Copy_System Set this bit ON to ask the system to copy these System Records from the PLC to the SD card:
Error History, Failed Password Attempts Record, Email Log, Allow List Denied Record.The bit will turn OFF when the action is completed. Read/Write
- SC68 _SD_Ready_To_Use ON when the SD card is "Ready to Use". Read
- SC69 _SD_Write_Status ON when writing data to the SD card. Read
- Note (pyrung CircuitPython codegen): `system.storage.sd.write_status` is emitted as a one-scan pulse when SD commands are serviced.
- SC70 _SD_Error ON when there is a SD card error. [SD69](system_data_registers.md) will contain the error information. Read
- SC75 _WLAN_Reset Set this bit ON to ask the system to Reconnect to the configured WLAN. ModbusTCP, MQTT and DHCP will be momentarily interrupted. The bit will turn OFF when the action is completed. Read/Write
- SC76 _Sub_CPU_Reset Set this bit ON to ask the system to Reset operation of the sub-systems: WLAN, Bluetooth, SD Card. WLAN will reconnect, Bluetooth will disconnect current session, SD Card will be remounted. ModbusTCP, MQTT and DHCP will be momentarily interrupted. The bit will turn OFF when the action is completed. Read/Write
- SC80 _WLAN_Ready_Flag ON when WLAN is Ready, does not indicate **Busy Status**. Read
- SC81 _WLAN_Error_Flag ON when there is a WLAN error. [SD213](system_data_registers.md) will contain the error information. Read
- SC82 _WLAN_Connection_Limit ON when all of WLAN server connections are busy. Read
- SC83 _WLAN_IP_Resolved ON when WLAN IP Address is assigned. Read
- SC84 _WLAN_Connected ON when WLAN is connected to the access point. Read
- SC86 _WLAN_DHCP_Enabled ON when WLAN is configured for DHCP. Read
- SC87 _WLAN_DNS_Success ON when WLAN DNS Lookup was successful. Read
- SC88 _WLAN_DNS_Error ON when WLAN DNS Lookup was an error. Read
- SC90 _Port_1_Ready_Flag ON when Port 1 is Ready, does not indicate **Busy Status**. Read
- SC91 _Port_1_Error_Flag ON when Port 1 has an **Error** with at least one server. Read
- SC92 _Port_1_Connection_Limit ON when all of Port 1 server connections are busy. Read
- SC93 _Port_1_IP_Resolved ON when Port 1 IP Address is assigned. Read
- SC94 _Port_1_Link_Flag ON when Port 1 Link is good. Read
- SC95 _Port_1_100MBIT_Flag ON when Port 1 Link is 100 Mbps. Read
- SC96 _Port_1_DHCP_Enabled ON when Port1 is configured for DHCP. Read
- SC97 _Port_1_DNS_Success ON when Port1 DNS Lookup was successful. Read
- SC98 _Port_1_DNS_Error ON when Port1 DNS Lookup was an error. Read
- SC100 _Port_2_Ready_Flag ON when Port 2 is Ready. Read
- SC101 _Port_2_Error_Flag ON when Port 2 has an Error. Read
- SC102 _Port_3_Ready_Flag ON when Port 3 is Ready. Read
- SC103 _Port_3_Error_Flag ON when Port 3 has an Error. Read
- SC111 _EIP_Con1_ConOnline **ON** when a **Class 1 Implicit** **Connection** is **active**. Read
- SC112 _EIP_Con1_Error **ON** when a **Class 1 Implicit** was **rejected** because of a configuration error. Read
- SC113 _EIP_Con1_Originator_Run **ON** when a **Class 1 Implicit** Connection is **active** and the **Originator** is including a **Status Header** indicating its **Run/Idle state**. Read
- SC114 _EIP_Con2_ConOnline **ON** when a **Class 1 Implicit** Connection is **active**. Read
- SC115 _EIP_Con2_Error **ON** when a **Class 1 Implicit** Connection was rejected because of a configuration error. Read
- SC116 _EIP_Con2_Originator_Run **ON** when a **Class 1 Implicit** Connection is **active** and the **Originator** is including a **Status Header** indicating its **Run/Idle state**. Read
- SC120 _Network Time_Request Set this bit ON to start a NTP Request. Read/Write
- SC121 _Network Time_DST Set this bit ON to add one hour to the current local time for Daylight Savings Time.Note: Using DST (Daylight Saving Time) adjustments along with Data Logging may cause anomalies in the records. When the system time is adjusted backwards records may be lost or appended to existing files. When the system time is adjusted forward a time gap will appear in the records. Read/Write
- SC122 _Network Time_Processing ON during a NTP Request. Read
- SC123 _Network Time_Error ON when the NTP Request had an Error. Read
- SC131 _Password_Failure_Detect ON when a Login failure has occurred and the [SD131](system_data_registers.md) count is not zero. Read
- SC132 _Password_Locked_Out ON when too many Login failures have occurred, please wait 10 seconds. Read
- SC133 _Port1_AL_Enabled ON when the Allow List feature is enabled on Port1. Read
- SC134 _Port1_AL_Denied_Flag ON when a connection has been rejected by the Allow List on Port1. Read
- SC135 _WLAN_AL_Enabled ON when the Allow List feature is enabled on WLAN Port. Read
- SC136 _WLAN_AL_Denied_Flag ON when a connection has been rejected by the Allow List on WLAN Port. Read
- SC140 _S0_P1_Ready_Flag ON when DCM0 (Slot 0) Port 1 is Ready. Read
- SC141 _S0_P1_Error_Flag ON when DCM0 (Slot 0) Port 1 has an Error. Read
- SC142 _S0_P2_Ready_Flag ON when DCM0 (Slot 0) Port 2 is Ready. Read
- SC143 _S0_P2_Error_Flag ON when DCM0 (Slot 0) Port 2 has an Error. Read
- SC144 _S1_P1_Ready_Flag ON when DCM1 (Slot 1) Port 1 is Ready. Read
- SC145 _S1_P1_Error_Flag ON when DCM1 (Slot 1) Port 1 has an Error. Read
- SC146 _S1_P2_Ready_Flag ON when DCM1 (Slot 1) Port 2 is Ready. Read
- SC147 _S1_P2_Error_Flag ON when DCM1 (Slot 1) Port 2 has an Error. Read
- SC150 _PTO_Axis1_Ready_Flag PTO Axis 1 Ready. Read
- SC151 _PTO_Axis2_Ready_Flag PTO Axis 2 Ready. Read
- SC152 _PTO_Axis3_Ready_Flag PTO Axis 3 Ready. Read
- SC202 _Fixed_Scan_Mode ON when the Fixed Scan Mode is selected. Read
- SC203 _Battery_Installed ON when Battery is Installed.Note: The CLICK CPU modules cannot detect if a Battery is Installed automatically. If a Battery is Installed but this bit is not ON, open the Battery Backup Setup window (Drop-down Menu: Setup > Battery Backup Setup) and check the radio button for Battery Installed on the top. Read
- SC301 _S0INT_Ready_Flag 1) Intelligent module in slot 0
2) Enabled state=ON
3) Turn on when the module is ready. Read
- SC302 _S0INT_Error_Flag Active when an error occurs on the C2 Intelligent module installed in slot 0. Read
- SC303 _S0INT_Reset Turn on to reboot the intelligent module in slot 0. Read/Write
- SC304 _S0INT_SD_EJECT Set this bit ON to ask the system prepare to safely remove the SD card from slot 0. The bit will turn OFF when the SD card is ready to be removed. Read/Write
- SC305 _S0INT_SD_Delete_All Not Implemented. N/A
- SC306 _S0INT_SD_Copy_System Not Implemented. N/A
- SC307 _S0INT_SD_Ready_To_Use On when the SD card in the Intelligent module in slot 0 is connected and can read and write. Read
- SC309 _S0INT_IP_Resolved The Slot 0 IP Address has been resolved. Read
- SC310 _S0INT_Link_Flag There is an active network connection with the Intelligent module in slot 0. Read
- SC311 _S0INT_100M_Bit_Flag The network connection is 100 MBIT on the Intelligent module in slot 0. Read
- SC312 _S0INT_DHCP_Enabled DHCP is enabled on the Intelligent module in slot 0. Read
- SC315 _S0INT_Application_Flag Turn on when the C2 intelligent application in slot 0 has been launched. Read
- SC321 _S1INT_Ready_Flag 1) Intelligent module in slot 1.
2) Enabled state=ON.
3) Turn on when the module is ready. Read
- SC322 _S1INT_Error_Flag Active when an error occurs on the C2 Intelligent module installed in slot 1. Read
- SC323 _S1INT_Reset Turn on to reboot the intelligent module in slot 1. Read/Write
- SC324 _S1INT_SD_EJECT Eject the SD card from the Intelligent module in slot 1. Read/Write
- SC325 _S1INT_SD_Delete_All Not Implemented. N/A
- SC326 _S1INT_SD_Copy_System Not Implemented. N/A
- SC327 _S1INT_SD_Ready_To_Use On when the SD card in the Intelligent module in slot 1 is connected and can read and write. Read
- SC329 _S1INT_IP_Resolved The Slot 1 IP Address has been resolved. Read
- SC330 _S1INT_Link_Flag There is an active network connection with the Intelligent module in slot 1. Read
- SC331 _S1INT_100M_Bit_Flag The network connection is 100 MBIT on the Intelligent module in slot 1. Read
- SC332 _S1INT_DHCP_ Enabled DHCP is enabled on the Intelligent module in slot 1. Read
- SC335 _S1INT_Application_Flag Turn on when the C2 intelligent application in slot 1 has been launched. Read

| ![](Resources/Notepad.gif) | Note: The Nicknames of all System Control Relays and System Data Registers start with an Underscore ( _ ) to indicate that they are System Nicknames. On the other hand, all user-defined **Nicknames** can not start with an Underscore. Therefore, System Nicknames can be easily identified by the Underscore at the beginning of the Nickname. |
| --- | --- |


 

[![Related Topics Link Icon](../Skins/Default/Stylesheets/Images/transparent.gif)Related Topics](javascript:void(0);)
