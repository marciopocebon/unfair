#! /usr/bin/env python3
"""
Generates graphs to validate the ns-3 implementation of BBR.

Graphs are based on Ray's paper:
https://www.cs.cmu.edu//~rware/assets/pdf/ware-imc2019.pdf
"""

import argparse
import os
from os import path

from matplotlib import pyplot as plt
import numpy as np

import cl_args
import utils


AVG_TPUT_KEY = "average throughput p/s-windowed-minRtt16"
PKT_SIZE_B = 1380


def get_avg_tputs(flp):
    """ Returns the average throughput for each flow. """
    with np.load(flp) as fil:
        return (
            utils.Sim(flp),
            [utils.safe_mean(fil[flw][AVG_TPUT_KEY]) for flw in fil.files])


def plot_f1b(flps, out_dir):
    """ Generate figure 1b from Ray's paper. """
    datapoints = [get_avg_tputs(flp) for flp in flps]
    # Sort the datapoint based on the simulation BDP.
    datapoints = sorted(datapoints, key=lambda datapoint: datapoint[0].queue_p)
    sims, all_ys = zip(*datapoints)

    tot_flws = len(all_ys[0])
    assert tot_flws == 2, \
        ("This script supports simulations with two flows, but the "
         f"provided simulation contains {tot_flws} flows!")

    # Create the x-values by converting each bottleneck queue size
    # into a multiple of the BDP.
    x_vals = [
        sim.queue_p / (
            utils.bdp_B(sim.bw_Mbps, 6 * sim.btl_delay_us) / PKT_SIZE_B)
        for sim in sims]

    plt.figure(figsize=(8, 3))
    # Plot a line for each flow. Reverse the flows so that BBR is
    # plotted first.
    for idx, y_vals in enumerate(reversed(list(zip(*all_ys)))):
        plt.plot(
            x_vals,
            # Convert from packets/second to Mbps.
            np.array(y_vals) * PKT_SIZE_B * 8 / 1e6,
            # Line with circle markers.
            "o-",
            # The first flow is BBR and the second is Cubic.
            label=("BBR" if idx == 0 else "Cubic"))

    plt.xscale("log", basex=2)
    plt.xticks(x_vals, [f"{x:.2f}" if x < 1 else str(round(x)) for x in x_vals])
    plt.xlabel("Queue Size (BDP)")
    plt.ylabel("Throughput (Mbps)")
    plt.ylim(bottom=0, top=sims[0].bw_Mbps)
    plt.legend()
    plt.tight_layout()
    plt.savefig(path.join(out_dir, "1b.pdf"))
    plt.close()


def plot_f1c(flps, out_dir):
    """ Generate figure 1c from Ray's paper. """


def main():
    """ This program's entrypoint. """
    # Parse command line arguments.
    psr = argparse.ArgumentParser(
        description="Visualize a simulation's features.")
    psr.add_argument(
        "--f1b",
        help=("The path to a directory contained parsed data files for figure "
              "1b."),
        required=True, type=str)
    psr.add_argument(
        "--f1c",
        help=("The path to a directory containing a parsed data file for figure "
              "1c."),
        required=True, type=str)
    psr, psr_verify = cl_args.add_out(psr)
    args = psr_verify(psr.parse_args())
    f1b = args.f1b
    f1c = args.f1c
    out_dir = args.out_dir
    assert path.exists(f1b), f"Directory does not exist: {f1b}"
    assert path.exists(f1c), f"Directory does not exist: {f1c}"
    if not path.exists(out_dir):
        os.makedirs(out_dir)

    plot_f1b((path.join(f1b, fln) for fln in os.listdir(f1b)), out_dir)
    plot_f1c((path.join(f1c, fln) for fln in os.listdir(f1c)), out_dir)


if __name__ == "__main__":
    main()