"""Run this with the OpenFFBoard plugged in to find its VID/PID."""
import pywinusb.hid as hid

devices = hid.find_all_hid_devices()
if not devices:
    print("No HID devices found.")
else:
    print(f"{'VID':>6}  {'PID':>6}  {'Product':<40}  {'Manufacturer'}")
    print("-" * 80)
    for d in devices:
        print(f"0x{d.vendor_id:04X}  0x{d.product_id:04X}  {d.product_name:<40}  {d.vendor_name}")
