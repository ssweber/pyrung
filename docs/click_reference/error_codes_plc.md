The table provided below includes a list of the CLICK PLC Errors along with causes, solutions, and other reference information. When an Error or a Warning occurs, the Error Code is stored in the system data register SD1.

- Error Code SC Bit* Error Name Category Causes Solutions
- 001 N/A Power Fail Error Input Voltage is less than 20VDC. Provide the correct nominal voltage of 24VDC. Recalculate the power budget and compare to your power supply available current.
- 101 SC20 I/O Module Error Error There are more than 8 I/O modules. A CLICK PLC system can support up to 8 I/O modules. Remove any excessive I/O modules.
- At least one I/O module was added to the CLICK PLC during operation. Power off the CLICK PLC and check the connection of the I/O modules. Then power on the CLICK PLC again. If the problem remains, connect the CLICK software to the PLC and check the System Configuration. If there is any I/O module that is not shown in the System Configuration, replace it.
- An I/O module has failed. Connect the CLICK software to the CLICK PLC and check the system configuration. If there is any I/O module that is used in the PLC system but not shown in the System Configuration window, replace the I/O module.
- 102 SC21 System Config Error Error The current system configuration does not match the configuration saved in the project file. Connect the CLICK software to the CLICK PLC and open the System Configuration window. Modify the current configuration of the CLICK PLC to match the configuration in the project file, or uncheck the ‘Start-up I/O Config Check’ option if you want to use the current configuration.
- 103 SC22 I/O Config Error Error At least one I/O module was removed from the CLICK PLC during operation. Power off the CLICK PLC and check the connection of the I/O modules. Then power on the CLICK PLC again. If the problem remains, connect the CLICK software to the PLC and check the System Configuration. If there is any I/O module that is not shown in the System Configuration, replace it.
- The CPU module can not access one or more I/O modules. Connect the CLICK software to the CLICK PLC and open the System Configuration window. If there is any I/O module that is used in the PLC system but not shown in the System Configuration window, replace the I/O module.
- 104 SC23 Memory Check Error Error There is a memory check error. Power cycle the CLICK PLC. If the same error occurs again, download the project again and/or try the ‘Reset to Factory Default’ command. If the same error still occurs, replace the CPU module.
- 105 SC24 Project File Error Error There is no project file in the CLICK PLC. Download a project file into the CLICK PLC.
- The project file stored in the CLICK PLC is corrupted. Download the project file into the CLICK PLC again.
- 106 SC25 Firmware Version Error Error The project file was written on a newer version of CLICK software. The firmware in the CLICK PLC is too old to execute the project. Connect the CLICK software to the CLICK PLC and update the firmware of the CPU module.
- 107 SC26 Watchdog Timer Error Error The PLC scan time exceeded the watchdog timer setup. Connect the CLICK software to the PLC and check the maximum PLC scan time and the watchdog timer setup.
- 108 SC26 Interrupt Watchdog Timer Error Error The PLC scan time excedded the watchdog timer setup. The watchdog timer was excedded while executing an Interrupt Program. Reduce the occurrence of Interrupts, or reduce the executing time of the Interrupt Programs to prevent this error.
- 109 SC31 Sub-processor Firmware Version Error Error The sub-processor contains a firmware version which does not match the main processor. Connect the CLICK software to the CLICK PLC and update the firmware of the CPU module.
- 201 SC27 Lost SRAM Data Warning The data in the SRAM was lost while the CLICK PLC was powered off. The CLICK CPU module does not have a battery back-up, but it has a capacitor that will hold memory for a few days. The data in the SRAM is lost if the CLICK PLC is powered off for long enough for the capacitor to discharge. In this case, the CLICK PLC initializes the data in the SRAM automatically. Power cycle the CLICK PLC to clear this Warning.
- 202 SC28 Battery Low Voltage Warning Battery voltage is too low to retain data in the SRAM. Replace the battery (ADC part #: D2-BAT-1).Also, set the new battery installation date and the anticipated replacement date in the CLICK programming software if the Battery Replacement Notification option is selected.(Pull-down menu: Setup > Battery Backup Setup)
- 203 SC29 Battery Replacement Warning The anticipated batteryreplacement date has passed. Replace the battery (ADC part #: D2-BAT-1). Also, set the new battery installation date and the anticipated replacement date in the CLICK programming software.(Pull-down menu: Setup > Battery Backup Setup)
- 204 SC30 Run Edit Project Error Warning The RUN Time Edit program download failed. The program download was not completed. The PLC will continue in RUN with the previous program.
- 205 SC32 C2-DCM FW Version Error Warning The C2-DCM contains a firmware version that is incompatible the main processor. Connect the CLICK software to the CLICK PLC and update the firmware of the CPU module.
- 301 X101 IO1
Module
Error Error The analog I/O module in I/O1
position is not functioning. Power cycle the CLICK PLC. If the same error occurs
again, replace the analog I/O module.
- 302 X201 IO2
Module
Error Error The analog I/O module in I/O2
position is not functioning. Power cycle the CLICK PLC. If the same error occurs
again, replace the analog I/O module.
- 303 X301 IO3
Module
Error Error The analog I/O module in I/O3
position is not functioning. Power cycle the CLICK PLC. If the same error occurs
again, replace the analog I/O module.
- 304 X401 IO4
Module
Error Error The analog I/O module in I/O4
position is not functioning. Power cycle the CLICK PLC. If the same error occurs
again, replace the analog I/O module.
- 305 X501 IO5
Module
Error Error The analog I/O module in I/O5
position is not functioning. Power cycle the CLICK PLC. If the same error occurs
again, replace the analog I/O module.
- 306 X601 IO6
Module
Error Error The analog I/O module in I/O6
position is not functioning. Power cycle the CLICK PLC. If the same error occurs
again, replace the analog I/O module.
- 307 X701 IO7
Module
Error Error The analog I/O module in I/O7
position is not functioning. Power cycle the CLICK PLC. If the same error occurs
again, replace the analog I/O module.
- 308 X801 IO8
Module
Error Error The analog I/O module in I/O8
position is not functioning. Power cycle the CLICK PLC. If the same error occurs
again, replace the analog I/O module.
- 310 X102 IO1
Missing
24V Warning The analog I/O module in I/O1
position is missing external
24VDC input. Apply 24 VDC to the analog I/O module.
- 311 X103 IO1 CH1
Burnout Warning CH1 on the analog I/O module in
I/O1 position senses burnout or
open circuit. Check the wiring for CH1. Replace the sensor if it is
broken.
- 312 X106 IO1 CH2
Burnout Warning CH2 on the analog I/O module in
I/O1 position senses burnout or
open circuit. Check the wiring for CH2. Replace the sensor if it is
broken.
- 313 X109 IO1 CH3
Burnout Warning CH3 on the analog I/O module in
I/O1 position senses burnout or
open circuit. Check the wiring for CH3. Replace the sensor if it is
broken.
- 314 X112 IO1 CH4
Burnout Warning CH4 on the analog I/O module in
I/O1 position senses burnout or
open circuit. Check the wiring for CH4. Replace the sensor if it is
broken.
- 320 X202 IO2
Missing
24V Warning The analog I/O module in I/O2
position is missing external
24VDC input. Apply 24 VDC to the analog I/O module.
- 321 X203 IO2 CH1
Burnout Warning CH1 on the analog I/O module in
I/O2 position senses burnout or
open circuit. Check the wiring for CH1. Replace the sensor if it is
broken.
- 322 X206 IO2 CH2
Burnout Warning CH2 on the analog I/O module in
I/O2 position senses burnout or
open circuit. Check the wiring for CH2. Replace the sensor if it is
broken.
- 323 X209 IO2 CH3
Burnout Warning CH3 on the analog I/O module in
I/O2 position senses burnout or
open circuit. Check the wiring for CH3. Replace the sensor if it is
broken.
- 324 X212 IO2 CH4
Burnout Warning CH4 on the analog I/O module in
I/O2 position senses burnout or
open circuit. Check the wiring for CH4. Replace the sensor if it is
broken.
- 330 X302 IO3
Missing
24V Warning The analog I/O module in I/O3
position is missing external 24VDC input. Apply 24 VDC to the analog I/O module.
- 331 X303 IO3 CH1
Burnout Warning CH1 on the analog I/O module in
I/O3 position senses burnout or
open circuit. Check the wiring for CH1. Replace the sensor if it is
broken.
- 332 X306 IO3 CH2
Burnout Warning CH2 on the analog I/O module in
I/O3 position senses burnout or
open circuit. Check the wiring for CH2. Replace the sensor if it is
broken.
- 333 X309 IO3 CH3
Burnout Warning CH3 on the analog I/O module in
I/O3 position senses burnout or
open circuit. Check the wiring for CH3. Replace the sensor if it is
broken.
- 334 X312 IO3 CH4
Burnout Warning CH4 on the analog I/O module in
I/O3 position senses burnout or
open circuit. Check the wiring for CH4. Replace the sensor if it is
broken.
- 340 X402 IO4
Missing
24V Warning The analog I/O module in I/O4
position is missing external 24
VDC input. Apply 24 VDC to the analog I/O module.
- 341 X403 IO4 CH1
Burnout Warning CH1 on the analog I/O module in
I/O4 position senses burnout or
open circuit. Check the wiring for CH1. Replace the sensor if it is
broken.
- 342 X406 IO4 CH2
Burnout Warning CH2 on the analog I/O module in
I/O4 position senses burnout or
open circuit. Check the wiring for CH2. Replace the sensor if it is
broken.
- 343 X409 IO4 CH3
Burnout Warning CH3 on the analog I/O module in
I/O4 position senses burnout or
open circuit. Check the wiring for CH3. Replace the sensor if it is
broken.
- 344 X412 IO4 CH4
Burnout Warning CH4 on the analog I/O module in
I/O4 position senses burnout or
open circuit. Check the wiring for CH4. Replace the sensor if it is
broken.
- 350 X502 IO5
Missing
24V Warning The analog I/O module in I/O5
position is missing external 24
VDC input Apply 24 VDC to the analog I/O module.
- 351 X503 IO5 CH1
Burnout Warning CH1 on the analog I/O module in
I/O5 position senses burnout or
open circuit. Check the wiring for CH1. Replace the sensor if it is
broken.
- 352 X506 IO5 CH2
Burnout Warning CH2 on the analog I/O module in
I/O5 position senses burnout or
open circuit. Check the wiring for CH2. Replace the sensor if it is
broken.
- 353 X509 IO5 CH3
Burnout Warning CH3 on the analog I/O module in
I/O5 position senses burnout or
open circuit Check the wiring for CH3. Replace the sensor if it is
broken.
- 354 X512 IO5 CH4
Burnout Warning CH4 on the analog I/O module in
I/O5 position senses burnout or
open circuit. Check the wiring for CH4. Replace the sensor if it is
broken.
- 360 X602 IO6
Missing
24V Warning The analog I/O module in I/O6
position is missing external
24VDC input. Apply 24 VDC to the analog I/O module.
- 361 X603 IO6 CH1
Burnout Warning CH1 on the analog I/O module in
I/O6 position senses burnout or
open circuit. Check the wiring for CH1. Replace the sensor if it is
broken.
- 362 X606 IO6 CH2
Burnout Warning CH2 on the analog I/O module in
I/O6 position senses burnout or
open circuit. Check the wiring for CH2. Replace the sensor if it is
broken.
- 363 X609 IO6 CH3
Burnout Warning CH3 on the analog I/O module in
I/O6 position senses burnout or
open circuit. Check the wiring for CH3. Replace the sensor if it is
broken.
- 364 X612 IO6 CH4
Burnout Warning CH4 on the analog I/O module in
I/O6 position senses burnout or
open circuit. Check the wiring for CH4. Replace the sensor if it is
broken.
- 370 X702 IO7
Missing
24V Warning The analog I/O module in I/O7
position is missing external 24
VDC input. Apply 24 VDC to the analog I/O module.
- 371 X703 IO7 CH1
Burnout Warning CH1 on the analog I/O module in
I/O7 position senses burnout or
open circuit. Check the wiring for CH1. Replace the sensor if it is
broken.
- 372 X706 IO7 CH2
Burnout Warning CH2 on the analog I/O module in
I/O7 position senses burnout or
open circuit. Check the wiring for CH2. Replace the sensor if it is
broken.
- 373 X709 IO7 CH3
Burnout Warning CH3 on the analog I/O module in
I/O7 position senses burnout or
open circuit. Check the wiring for CH3. Replace the sensor if it is
broken.
- 374 X712 IO7 CH4
Burnout Warning CH4 on the analog I/O module in
I/O7 position senses burnout or
open circuit. Check the wiring for CH4. Replace the sensor if it is
broken.
- 380 X802 IO8
Missing
24V Warning The analog I/O module in I/O8
position is missing external 24
VDC input. Apply 24 VDC to the analog I/O module.
- 381 X803 IO8 CH1
Burnout Warning CH1 on the analog I/O module in
I/O8 position senses burnout or
open circuit. Check the wiring for CH1. Replace the sensor if it is
broken.
- 382 X806 IO8 CH2
Burnout Warning CH2 on the analog I/O module in
I/O8 position senses burnout or
open circuit. Check the wiring for CH2. Replace the sensor if it is
broken.
- 383 X809 IO8 CH3
Burnout Warning CH3 on the analog I/O module in
I/O8 position senses burnout or
open circuit. Check the wiring for CH3. Replace the sensor if it is
broken.
- 384 X812 IO8 CH4
Burnout Warning CH4 on the analog I/O module in
I/O8 position senses burnout or
open circuit. Check the wiring for CH4. Replace the sensor if it is
broken.
- Note: *The SCbits are turned ON when the related Errors occur.
