**Legend:**
*   **Y** = Yes Compatible
*   **N** = Not Compatible

### Part 1: Register Section
| Data Type | XD (Hex) | YD (Hex) | TD (Int) | CTD (Int2) | DS (Int) | DD (Int2) | DF (Float) | DH (Hex) | SD (Int) | TXT |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
Reference
| **XD (Hex)** | Y | Y | N | N | N | N | N | Y | N | N |
| **YD (Hex)** | Y | Y | N | N | N | N | N | Y | N | N |
| **TD (Int)** | N | N | Y | Y | Y | Y | Y | N | Y | N |
| **CTD (Int2)** | N | N | Y | Y | Y | Y | Y | N | Y | N |
| **DS (Int)** | N | N | Y | Y | Y | Y | Y | N | Y | N |
| **DD (Int2)** | N | N | Y | Y | Y | Y | Y | N | Y | N |
| **DF (Float)** | N | N | Y | Y | Y | Y | Y | N | Y | N |
| **DH (Hex)** | Y | Y | N | N | N | N | N | Y | N | N |
| **SD (Int)** | N | N | Y | Y | Y | Y | Y | N | Y | N |
| **TXT** | N | N | N | N | N | N | N | N | N | Y |
Constant
| **Int (1 Word)** | N | N | Y | Y | Y | Y | Y | N | Y | N |
| **Int (2 Words)** | N | N | Y | Y | Y | Y | Y | N | Y | N |
| **Floating Point**| N | N | Y | Y | Y | Y | Y | N | Y | N |
| **HEX (1 Word)** | Y | Y | N | N | N | N | N | Y | N | N |
| **Text** | N | N | N | N | N | N | N | N | N | Y |

### Part 2: Constant Section
| Data Type | Integer (1 Word) | Integer (2 Words) | Floating Point | HEX (1 Word) | Text |
| :--- | :---: | :---: | :---: | :---: | :---: |
Register
| **XD (Hex)** | N | N | N | Y | N |
| **YD (Hex)** | N | N | N | Y | N |
| **TD (Int)** | Y | Y | Y | N | N |
| **CTD (Int2)** | Y | Y | Y | N | N |
| **DS (Int)** | Y | Y | Y | N | N |
| **DD (Int2)** | Y | Y | Y | N | N |
| **DF (Float)** | Y | Y | Y | N | N |
| **DH (Hex)** | N | N | N | Y | N |
| **SD (Int)** | Y | Y | Y | N | N |
| **TXT** | N | N | N | N | Y |
Constant
| **Int (1 Word)** | Y | Y | Y | N | N |
| **Int (2 Words)** | Y | Y | Y | N | N |
| **Floating Point**| Y | Y | Y | N | N |
| **HEX (1 Word)** | N | N | N | Y | N |
| **Text** | N | N | N | N | Y |


### Related Topics:

[Compare Contact](contact_compare.md)
