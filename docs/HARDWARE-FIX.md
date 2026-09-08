# The hidden i2c@ff150000 bug

## Discovery

The PinePhone Pro PineDio SX1262 back cover was thought to be unreachable from postmarketOS. Every scan of every I2C bus came up silent. `i2cdetect -y 5` showed `--` at every address. After multiple reseats of the back cover, the bus stayed dead.

## Root cause

postmarketOS v26.06 ships the megi kernel for PinePhone Pro with the I2C controller at `i2c@ff150000` set to `status = "disabled"` in the device tree. This controller corresponds to `/dev/i2c-5` on the running system. The PineDio back cover's ATtiny84 I2C-to-SPI bridge lives on this exact bus at address `0x28`. With the controller disabled, no device on the bus can respond.

The bug: even when the back cover is physically seated and the ATtiny84 firmware is correctly loaded, the kernel refuses to bring up the I2C bus. `i2cdetect` runs but the physical lines never toggle.

## Confirming the diagnosis

Decompile the stock DTB:

```bash
sudo dtc -@ -I dtb -O dts -o /tmp/rk3399-pinephone-pro.dts /boot/rk3399-pinephone-pro.dtb
grep -A 15 "i2c@ff150000" /tmp/rk3399-pinephone-pro.dts
```

Output:

```
i2c@ff150000 {
    compatible = "rockchip,rk3399-i2c";
    reg = <0x00 0xff150000 0x00 0x1000>;
    ...
    pinctrl-names = "default";
    pinctrl-0 = <0x54>;
    #address-cells = <0x01>;
    #size-cells = <0x00>;
    status = "disabled";
};
```

`status = "disabled"` is the smoking gun.

Also: `/sys/class/i2c-adapter/i2c-5/` exists in sysfs (the driver is bound) but the bus shows nothing on the wire. Contrast with `/dev/i2c-9` (the i2c-4-mux) where the rk818 PMIC responds at 0x1c.

## Fix

1. Change `status = "disabled"` to `status = "okay"` on the `i2c@ff150000` node.
2. Add a documentation child node `lora_back@28` with `compatible = "pine64,pinedio-lora"`, `reg = <0x28>`. This is just metadata; the ATtiny84 bridge is driven entirely from userspace over smbus2.
3. Recompile the DTB: `sudo dtc -@ -I dts -O dtb -o /boot/rk3399-pinephone-pro-pinedio.dts /tmp/rk3399-pinephone-pro.dts`
4. Point GRUB at the patched DTB:
   ```
   sudo sed -i 's|devicetree /rk3399-pinephone-pro.dtb|devicetree /rk3399-pinephone-pro-pinedio.dtb|' /boot/grub/grub.cfg
   ```
5. Reboot.

## After fix

`i2cdetect -y 5` now shows:

```
20: -- -- -- -- -- -- -- -- 28 -- -- -- -- -- -- --
```

The PineDio ATtiny84 bridge is alive on `/dev/i2c-5:0x28`. From here, the SX1262 LoRa radio is reachable through the bridge via smbus2 from userspace. No kernel module needed.

## Why this hasn't been fixed upstream

postmarketOS uses the megi kernel patches for the PinePhone Pro. megi's PineDio support was developed for the original PinePhone (A64 SoC), where the SX1262 sits on a different bus topology. The PinePhone Pro (RK3399) was added later with the I2C controller defaulting to disabled. Nobody reported this on the postmarketOS tracker because the typical workaround is "buy a USB SX1262 dongle" which sidesteps the pogo-pin bridge entirely.

The fix is one line: `status = "okay"` instead of `disabled`. Two minutes of DTB editing. Should be filed as an upstream patch to pmaports.

## Lessons

- Always check the device tree status fields for I2C/SPI controllers before assuming a peripheral is dead. A disabled controller looks identical to a missing device at the i2cdetect level (silence on every address).
- The PineDio back cover on PinePhone Pro is reachable only through the I2C bridge, not raw SPI. The kernel's `CONFIG_SPI_SPIDEV=y` is irrelevant for this hardware.
- Userspace drivers on Alpine postmarketOS are feasible because the ATtiny84 bridge protocol is documented and simple (write-then-read transactions over I2C, length-prefixed payload).
