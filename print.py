#!/usr/bin/env python
import argparse
import asyncio
import logging
import sys
import os

from catprinter import logger
from catprinter.cmds import PRINT_WIDTH
from catprinter.ble import run_ble
from catprinter.img import read_img, read_img_gray_levels, show_preview
from catprinter.zpl import is_zpl, zpl_to_img


def parse_args():
    args = argparse.ArgumentParser(
        description='prints an image or a ZPL label on your cat thermal printer')
    args.add_argument('filename', type=str)
    args.add_argument('-l', '--log-level', type=str,
                      choices=['debug', 'info', 'warn', 'error'], default='info')
    args.add_argument('-b', '--img-binarization-algo', type=str,
                      choices=['mean-threshold',
                               'floyd-steinberg', 'atkinson', 'halftone', 'none'],
                      default='floyd-steinberg',
                      help=f'Which image binarization algorithm to use. If \'none\'  \
                             is used, no binarization will be used. In this case the \
                             image has to have a width of {PRINT_WIDTH} px.')
    args.add_argument('-s', '--show-preview', action='store_true',
                      help='If set, displays the final image and asks the user for \
                          confirmation before printing.')
    args.add_argument('-d', '--device', type=str, default='',
                      help=(
                          'The printer\'s Bluetooth Low Energy (BLE) address '
                          '(MAC address on Linux; UUID on macOS) '
                          'or advertisement name (e.g.: "GT01", "GB02", "GB03", "MXW01"). '
                          'If omitted, the the script will try to auto discover '
                          'the printer based on its advertised BLE services.'
                      ))
    args.add_argument('-g', '--grayscale', action='store_true',
                      help='Print with 16 real gray levels instead of dithered black and white. '
                           'Only the MXW01 supports it; other models fall back to dithering. '
                           'Ignored for ZPL labels.')
    args.add_argument('--zpl-layout', choices=['reflow', 'scale'], default='reflow',
                      help='How to fit ZPL labels wider than the paper (e.g. 10 cm shipping '
                           'labels): "reflow" rebuilds them as a single column, keeping text '
                           'and QR codes at their original size; "scale" shrinks them as drawn.')
    args.add_argument('-e', '--energy', type=lambda h: int(h.removeprefix("0x"), 16),
                      help="Thermal energy. Between 0x0000 (light) and 0xffff (darker, default).",
                      default="0xffff")
    return args.parse_args()


def configure_logger(log_level):
    logger.setLevel(log_level)
    h = logging.StreamHandler(sys.stdout)
    h.setLevel(log_level)
    logger.addHandler(h)


def main():
    args = parse_args()

    log_level = getattr(logging, args.log_level.upper())
    configure_logger(log_level)

    filename = args.filename
    if not os.path.exists(filename):
        logger.info('🛑 File not found. Exiting.')
        return

    try:
        if is_zpl(filename):
            # ZPL labels are drawn 1:1 in printer dots; no resizing or dithering.
            with open(filename, encoding='utf-8', errors='replace') as f:
                bin_img = zpl_to_img(f.read(), PRINT_WIDTH, args.zpl_layout)
        else:
            bin_img = read_img(
                args.filename,
                PRINT_WIDTH,
                args.img_binarization_algo,
            )
        if args.show_preview:
            show_preview(bin_img)
    except RuntimeError as e:
        logger.error(f'🛑 {e}')
        return

    logger.info(f'✅ Read image: {bin_img.shape} (h, w) pixels')
    gray_levels = None
    if args.grayscale and not is_zpl(filename):
        gray_levels = read_img_gray_levels(filename, PRINT_WIDTH)

    # Try to autodiscover a printer if --device is not specified.
    asyncio.run(run_ble(bin_img, energy=args.energy, device=args.device,
                        gray_levels=gray_levels))


if __name__ == '__main__':
    main()
