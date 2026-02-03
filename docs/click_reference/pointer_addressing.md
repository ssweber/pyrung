

| Pointer Addressing | Topic: CL238<br>![](Resources/ClickLogo.gif) |
| --- | --- |


## PointerAddressingIn certain cases, the CLICK PLC allows the use of Pointer Addressing for flexibility in programming.Only the DS Memory Type can be used as Pointer. Pointer Addressing uses the Pointer's data value to Point to a Memory location within the range of one of the eligible Memory types. Pointer Addressing can be used for the **C**, DS, DD, DH, DF, XD, YD, TD, CTD and TXT Memory types.Pointer Addressing Example 1DS1 = 100DD[DS1] means DD100In the above example, DS1 is a Pointer. DD[DS1] is called Pointer Addressing. DD[DS1] is identical to DD100 in this case.Important: Currently, only the Copy instruction supports Pointer Addressing in the Single Copy mode. The Pointer Addressing can be used for the Source and/or Destinationas shown below. Pointer Addressing Example 2Using Pointer Addressing with a For-Next Loop to move a block of memory. Although Pointer Addressing is only available in the Single Copy mode, the For-Next Loop can be applied for more flexibility. This example will move 100 Bits from Source C100-C199 into Destination C200-C299.Related Topics:[Single Copy](copy_single.md) [Block Copy](copy_block.md) [Pack Copy](copy_pack.md) [Unpack Copy](copy_unpack.md)
