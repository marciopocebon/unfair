#!/usr/bin/env python3
"""
Based on:
- https://pytorch.org/tutorials/beginner/blitz/cifar10_tutorial.html
- https://blog.floydhub.com/long-short-term-memory-from-zero-to-hero-with-pytorch/
"""

import argparse
import functools
import json
import math
import multiprocessing
import os
from os import path
import random
import time

import numpy as np
import torch

import models
import utils


# Parameter defaults.
DEFAULTS = {
    "epochs": 100,
    "num_gpus": 0,
    "train_batch": 10,
    "test_batch": 10_000,
    "learning_rate": 0.001,
    "momentum": 0.9,
    "early_stop": False,
    "val_patience": 10,
    "val_improvement_thresh": 0.1,
    "conf_trials": 1,
    "max_attempts": 10,
    "no_rand": False,
    "timeout_s": -1,
    "out_dir": "."
}
# The maximum number of epochs when using early stopping.
EPCS_MAX = 10_000
# The threshold of the new throughout to the old throughput above which a
# a training example will not be considered. I.e., the throughput must have
# decreased to less than this fraction of the original throughput for a
# training example to be considered.
NEW_TPT_TSH = 0.925
# Whether to generate training data graphs.
PLT = False
# The random seed.
SEED = 1337
# The number of validation passes per epoch.
VALS_PER_EPC = 15
# The number of output classes.
NUM_CLASSES = 5


class Dataset(torch.utils.data.Dataset):
    """ A simple Dataset that wraps an array of (input, output) value pairs. """

    def __init__(self, dat):
        super(Dataset).__init__()
        self.dat = dat

    def __len__(self):
        """ Returns the number of items in this Dataset. """
        return len(self.dat)

    def __getitem__(self, idx):
        """ Returns a specific item from this Dataset. """
        assert torch.utils.data.get_worker_info() is None, \
            "This Dataset does not support being loaded by multiple workers!"
        return self.dat[idx]


def match(a, b):
    """
    Merges two data matrices that both use sequence number as the
    primary key (column 0). The output matrix will contain only
    sequence numbers that are present in a. This function assumes that
    order is preserved between a and b, but that b may have additional
    entries. Every entry in a must have a corresponding entry in b.
    """
    # Create an empty matrix that is the size of a but has extra
    # columns for the corresponding entries from b. Discard the first
    # column of b, which is the sequence number.
    merged = np.empty(a.shape, dtype=a.dtype.descr + b.dtype.descr[1:])
    rows_a = a.shape[0]
    cols_a = len(a.dtype.names)
    rows_b = b.shape[0]
    cols_b = len(b.dtype.names)
    i_a = 0
    i_b = 0
    while i_a < rows_a and i_b < rows_b:
        if a[i_a][0] == b[i_b][0]:
            # The current entry in b is a match for the current entry
            # in a. Fill in the current output row.
            for j in range(cols_a):
                merged[i_a][j] = a[i_a][j]
            for j in range(1, cols_b):
                merged[i_a][cols_a + j - 1] = b[i_b][j]
            i_a += 1
            i_b += 1
        else:
            # The current entry in b does not match. Skip it.
            i_b += 1
    assert i_a == rows_a, "Did not match all rows in a."
    return merged


def scale_fets(dat_all):
    """
    dat_all is a list of numpy arrays, each corresponding to a simulation.
    Returns a copy of dat_all with the columns scaled between 0 and 1. Also
    returns an array of shape (dat_all[0].shape[1], 2) where row i contains the
    min and the max of column i in the entries in dat_all.
    """
    assert dat_all, "No data!"
    # Pick the first simulation result and determine the column specification.
    fets = dat_all[0].dtype.names
    # Create an empty array to hold the min and max values for each feature.
    scl_prms = np.empty((len(fets), 2))
    # First, look at all features across simulations to determine the
    # global scale paramters for each feature. For each feature column...
    for j, fet in enumerate(fets):
        # Find the global min and max values for this feature.
        min_global = float("inf")
        max_global = float("-inf")
        # For each simulation...
        for dat in dat_all:
            fet_values = dat[fet]
            min_global = min(min_global, fet_values.min())
            max_global = max(max_global, fet_values.max())
        scl_prms[j] = np.array([min_global, max_global])

    def normalize(dat):
        """ Rescale all of the features in dat to the range [0, 1]. """
        nrm = np.empty(dat.shape, dtype=dat.dtype)
        for j, fet in enumerate(fets):
            min_in, max_in = scl_prms[j]
            fet_values = dat[fet]
            if min_in == max_in:
                scaled = np.zeros(fet_values.shape, dtype=fet_values.dtype)
            else:
                scaled = utils.scale(
                    fet_values, min_in, max_in, min_out=0, max_out=1)
            nrm[fet] = scaled
        return nrm

    return [normalize(dat) for dat in dat_all], scl_prms


def convert_to_class(dat_all):
    """
    Converts real-valued feature values into classes. dat_all is a
    list of pairs, one for each simulation, of:
        (feature matrix, number of flows)
    """
    for dat, _ in dat_all:
        assert len(dat.dtype.names) == 1, "Should be only one column."

    def percent_to_class(prc, num_flws):
        """ Convert a queue occupancy percent to a fairness class. """
        assert len(prc) == 1, "Should be only one column."
        prc = prc[0]

        # The fair queue occupancy.
        fair = 1. / num_flws
        # Threshold between fair and unfair.
        tsh_fair = 0.1
        # Threshold between unfair and very unfair.
        tsh_unfair = 0.4

        dif = (fair - prc) / fair
        if dif < -1 * tsh_unfair:
            # We are much lower than fair.
            cls = 0
        elif -1 * tsh_unfair <= dif < -1 * tsh_fair:
            # We are not that much lower than fair.
            cls = 1
        elif -1 * tsh_fair <= dif <= tsh_fair:
            # We are fair.
            cls = 2
        elif tsh_fair < dif <= tsh_unfair:
            # We are not that much higher than fair.
            cls = 3
        elif tsh_unfair < dif:
            # We are much higher than fair.
            cls = 4
        else:
            assert False, "This should never happen."
        return cls

    return [
        np.vectorize(
            functools.partial(percent_to_class, num_flws=num_flws),
            otypes=[int])(dat)
        for dat, num_flws in dat_all]


def parse_sim(sim):
    """ Parse one pair of simulation result files and merge them. """
    print(f"    Parsing: {sim}")
    _, _, _, unfair_flows, other_flows, _, _, _, _ = sim.split("-")
    with np.load(f"{sim}-csv.npz") as fil_csv:
        dat_csv = fil_csv[fil_csv.files[0]]
    with np.load(f"{sim}-fairness.npz") as fil_fair:
        dat_fair = fil_fair[fil_fair.files[0]]
    return (int(unfair_flows[:-6]) + int(other_flows[:-5]),
            match(dat_csv, dat_fair))


def make_datasets(dat_dir, in_spc, out_spc, bch_trn, bch_tst):
    """
    Parses the files in data_dir, transforms them (e.g., by scaling)
    into the correct format for the network, and returns training,
    validation, and test data loaders.
    """
    print("Loading data...")
    # Find all simulations.
    sims = set()
    for fln in os.listdir(dat_dir):
        sim, _ = fln.split(".")
        if sim.endswith("csv"):
            # Remove "-csv" and remember this simulation.
            sims = sims | {sim[:-4],}
    sims = set(list(sims))
    print(f"    Found {len(sims)} simulations.")
    with multiprocessing.Pool() as pol:
        # Each element of dat corresponds to a single simulation.
        dat = pol.map(parse_sim, [path.join(dat_dir, sim) for sim in sims])
    print("Done.")

    print("Formatting data...")
    assert in_spc, "Empty in spec."
    assert out_spc, "Empty out spec."
    # Split each data matrix into two separate matrices: one with the input
    # features only and one with the output features only. The names of the
    # columns correspond to the feature names in in_spc and out_spc.
    dat = [(num_flws_, d[in_spc], d[out_spc]) for num_flws_, d in dat]
    # Unzip dat from a list of pairs of in and out features into a pair of lists
    # of in and out features.
    num_flws, dat_in, dat_out = zip(*dat)
    # Scale input features.
    dat_in, prms_in = scale_fets(dat_in)
    # Convert output features to class labels. Must call list()
    # because the value gets used more than once.
    dat_out = convert_to_class(list(zip(dat_out, num_flws)))

    # Verify data.
    for (d_in, d_out) in zip(dat_in, dat_out):
        assert d_in.shape[0] == d_out.shape[0], \
            "Should have the same number of rows."

    # Visualaize the ground truth data.
    def find_out(x):
        """ Returns the number of entries that have a particular value. """
        return sum([1 if v == x else 0
                    for d in dat_out
                    for v in d.tolist()])
    tot_0 = find_out(0)
    tot_1 = find_out(1)
    tot_2 = find_out(2)
    tot_3 = find_out(3)
    tot_4 = find_out(4)
    tot = tot_0 + tot_1 + tot_2 + tot_3 + tot_4
    print("    Ground truth:")
    print(f"        0 - much lower than fair: {tot_0} packets ({tot_0 / tot * 100:.2f}%)")
    print(f"        1 - lower than fair: {tot_1} packets ({tot_1 / tot * 100:.2f}%)")
    print(f"        2 - fair: {tot_2} packets ({tot_2 / tot * 100:.2f}%)")
    print(f"        3 - greater than fair: {tot_3} packets ({tot_3 / tot * 100:.2f}%)")
    print(f"        4 - much greater than fair: {tot_4} packets ({tot_4 / tot * 100:.2f}%)")
    assert (tot_0 + tot_1 + tot_2 + tot_3 + tot_4 ==
            sum([d.shape[0] for d in dat_out])), \
            "Error visualizing ground truth!"

    # Convert each training input/output pair to Torch tensors.
    dat = [(torch.tensor(d_in.tolist(), dtype=torch.float),
            torch.tensor(d_out, dtype=torch.long))
           for d_in, d_out in zip(dat_in, dat_out)]

    # Shuffle the data to ensure that the training, validation, and test sets
    # are uniformly sampled.
    random.shuffle(dat)
    # 50% for training, 20% for validation, 30% for testing.
    tot = len(dat)
    num_val = int(round(tot * 0.2))
    num_tst = int(round(tot * 0.3))
    print((f"    Data - train: {tot - num_val - num_tst}, val: {num_val}, "
           f"test: {num_tst}"))
    dat_val = dat[:num_val]
    dat_tst = dat[num_val:num_val + num_tst]
    dat_trn = dat[num_val + num_tst:]
    # Create the dataloaders.
    ldr_trn = torch.utils.data.DataLoader(
        Dataset(dat_trn), batch_size=bch_trn, shuffle=True)
    ldr_val = torch.utils.data.DataLoader(
        Dataset(dat_val), batch_size=bch_tst, shuffle=False)
    ldr_tst = torch.utils.data.DataLoader(
        Dataset(dat_tst), batch_size=bch_tst, shuffle=False)
    print("Done.")
    return ldr_trn, ldr_val, ldr_tst, prms_in


def init_hidden(net, bch, dev):
    """
    Initialize the hidden state. The hidden state is what gets built
    up over time as the LSTM processes a sequence. It is specific to a
    sequence, and is different than the network's weights. It needs to
    be reset for every new sequence.
    """
    hidden = (net.module.init_hidden if isinstance(net, torch.nn.DataParallel)
              else net.init_hidden)(bch)
    hidden[0].to(dev)
    hidden[1].to(dev)
    return hidden


def inference(ins, labs, net, dev, hidden, los_fnc=None):
    """
    Runs a single inference pass. Returns the output of net, or the
    loss if los_fnc is not None.
    """
    # Move the training data to the specified device.
    ins = ins.to(dev)
    labs = labs.to(dev)
    # LSTMs want the sequence length to be first and the batch
    # size to be second, so we need to flip the first and
    # second dimensions:
    #   (batch size, sequence length, LSTM.in_dim) to
    #   (sequence length, batch size, LSTM.in_dim)
    ins = ins.transpose(0, 1)
    # Reduce the labels to a 1D tensor.
    # TODO: Explain this better.
    labs = labs.transpose(0, 1).view(-1)
    # The forwards pass.
    out, hidden = net(ins, hidden)
    if los_fnc is None:
        return out, hidden
    return los_fnc(out, labs), hidden


def train(net, num_epochs, ldr_trn, ldr_val, dev, ely_stp,
          val_pat_max, out_flp, lr, momentum, val_imp_thresh,
          tim_out_s):
    """ Trains a model. """
    print("Training...")
    # Cross-entropy loss is designed for multi-class classification tasks.
    los_fnc = torch.nn.CrossEntropyLoss()
    opt = torch.optim.Adam(net.parameters(), lr=lr)
    # If using early stopping, then this is the lowest validation loss
    # encountered so far.
    los_val_min = None
    # If using early stopping, then this tracks the *remaining* validation
    # patience (initially set to the maximum amount of patience). This is
    # decremented for every validation pass that does not improve the
    # validation loss by at least val_imp_thresh percent. When this reaches
    # zero, training aborts.
    val_pat = val_pat_max

    # To avoid a divide-by-zero error below, the number of validation passes
    # per epoch much be at least 2.
    assert VALS_PER_EPC >= 2, "Must validate at least twice per epoch."
    if len(ldr_trn) < VALS_PER_EPC:
        # If the number of batches per epoch is less than the desired number of
        # validation passes per epoch, then do a validation pass after every
        # batch.
        bchs_per_val = 1
    else:
        # Using floor() means that dividing by (VALS_PER_EPC - 1) will result in
        # VALS_PER_EPC validation passes per epoch.
        bchs_per_val = math.floor(len(ldr_trn) / (VALS_PER_EPC - 1))
    print(f"Will validate after every {bchs_per_val} batches.")

    tim_srt_s = time.time()
    # Loop over the dataset multiple times...
    for epoch_idx in range(num_epochs):
        tim_del_s = time.time() - tim_srt_s
        if tim_out_s != -1 and tim_del_s > tim_out_s:
            print((f"Training timed out after after {epoch_idx} epochs "
                   f"({tim_del_s:.2f} seconds)."))
            break

        # For each batch...
        for bch_idx_trn, (ins, labs) in enumerate(ldr_trn, 0):
            print(f"Epoch: {epoch_idx + 1}/{'?' if ely_stp else num_epochs}, "
                  f"batch: {bch_idx_trn + 1}/{len(ldr_trn)}")
            # Initialize the hidden state for every new sequence.
            hidden = init_hidden(net, bch=ins.size()[0], dev=dev)
            hidden[0].to(dev)
            hidden[1].to(dev)
            # Zero out the parameter gradients.
            opt.zero_grad()
            loss, hidden = inference(ins, labs, net, dev, hidden, los_fnc)
            # The backward pass.
            loss.backward()
            opt.step()
            print(f"    Training loss: {loss:.5f}")

            # Run on validation set, print statistics, and (maybe) checkpoint
            # every VAL_PER batches.
            if ely_stp and not bch_idx_trn % bchs_per_val:
                print("    Validation pass:")
                # For efficiency, convert the model to evaluation mode.
                net.eval()
                with torch.no_grad():
                    los_val = 0
                    for bch_idx_val, (ins_val, labs_val) in enumerate(ldr_val):
                        print(f"    Validation batch: {bch_idx_val + 1}/{len(ldr_val)}")
                        # Initialize the hidden state for every new sequence.
                        hidden = init_hidden(net, bch=ins.size()[0], dev=dev)
                        hidden[0].to(dev)
                        hidden[1].to(dev)
                        los_val += inference(
                            ins_val, labs_val, net, dev, hidden, los_fnc)[0].item()
                # Convert the model back to training mode.
                net.train()

                if los_val_min is None:
                    los_val_min = los_val
                # Calculate the percent improvement in the validation loss.
                prc = (los_val_min - los_val) / los_val_min * 100
                print(f"    Validation error improvement: {prc:.2f}%")

                # If the percent improvement in the validation loss is greater
                # than a small threshold, then take this as the new best version
                # of the model.
                if prc > val_imp_thresh:
                    # This is the new best version of the model.
                    los_val_min = los_val
                    # Reset the validation patience.
                    val_pat = val_pat_max
                    # # Save the new best version of the model. Convert the
                    # # model to Torch Script first.
                    # torch.jit.save(torch.jit.script(net), out_flp)
                else:
                    val_pat -= 1
                    # if path.exists(out_flp):
                    #     # Resume training from the best model.
                    #     net = torch.jit.load(out_flp)
                    #     net.to(dev)
                if val_pat <= 0:
                    print(f"Stopped after {epoch_idx + 1} epochs")
                    return net
    # if not ely_stp:
    #     # Save the final version of the model. Convert the model to Torch Script
    #     # first.
    #     print(f"Saving: {out_flp}")
    #     torch.jit.save(torch.jit.script(net), out_flp)
    return net


def test(net, ldr_tst, dev):
    """ Tests a model. """
    print("Testing...")
    # The number of testing samples that were predicted correctly.
    num_correct = 0
    # Total testing samples.
    total = 0
    # For efficiency, convert the model to evaluation mode.
    net.eval()
    with torch.no_grad():
        for bch_idx, (ins, labs) in enumerate(ldr_tst):
            print(f"Test batch: {bch_idx + 1}/{len(ldr_tst)}")
            bch_tst, seq_len, _ = ins.size()
            # Initialize the hidden state for every new sequence.
            hidden = init_hidden(net, bch=ins.size()[0], dev=dev)
            hidden[0].to(dev)
            hidden[1].to(dev)
            # Run inference. The first element of the output is the
            # number of correct predictions.
            num_correct += inference(
                ins, labs, net, dev, hidden,
                los_fnc=lambda a, b: (
                    # argmax(): The class is the index of the output
                    #     entry with greatest value (i.e., highest
                    #     probability). dim=1 because the output has an
                    #     entry for every entry in the input sequence.
                    # eq(): Compare the outputs to the labels.
                    # type(): Cast the resulting bools to ints.
                    # sum(): Sum them up to get the total number of correct
                    #     predictions.
                    torch.argmax(
                        a, dim=1).eq(b).type(torch.IntTensor).sum().item()))[0]
            total += bch_tst * seq_len
    # Convert the model back to training mode.
    net.train()
    acc_tst = num_correct / total
    print(f"Test accuracy: {acc_tst * 100:.2f}%")
    return acc_tst


def run(args_):
    """
    Trains a model according to the supplied parameters. Returns the test error
    (lower is better).
    """
    # Initially, accept all default values. Then, override the defaults with
    # any manually-specified values. This allows the caller to specify values
    # only for parameters that they care about while ensuring that all
    # parameters have values.
    args = DEFAULTS
    args.update(args_)
    if args["early_stop"]:
        args["epochs"] = EPCS_MAX
    print(f"Arguments: {args}")

    if args["no_rand"]:
        random.seed(SEED)
        torch.manual_seed(SEED)

    out_dir = args["out_dir"]
    if not path.isdir(out_dir):
        os.makedirs(out_dir)
    out_flp = path.join(args["out_dir"], "net.pth")
    # If a trained model file already exists, then delete it.
    if path.exists(out_flp):
        os.remove(out_flp)

    # Instantiate and configure the network.
    net = models.MODELS[args["model"]]()
    num_gpus = torch.cuda.device_count()
    num_gpus_to_use = args["num_gpus"]
    if num_gpus >= num_gpus_to_use > 1:
        net = torch.nn.DataParallel(net)
        # Do this after converting the net to a DataParallel() in case
        # that process somehow changes the in_spc. I.e., we want to
        # get the input and output specs from the *final* network
        # object.
        in_spc = net.module.in_spc
        out_spc = net.module.out_spc
    else:
        in_spc = net.in_spc
        out_spc = net.out_spc

    dev = torch.device(
        "cuda:0" if num_gpus >= num_gpus_to_use > 0 else "cpu")
    net.to(dev)

    bch_trn = args["train_batch"]
    ldr_trn, ldr_val, ldr_tst, scl_prms = make_datasets(
        args["data_dir"], in_spc, out_spc, bch_trn, args["test_batch"])

    # Save scaling parameters.
    with open(path.join(out_dir, "scale_params.json"), "w") as fil:
        json.dump(scl_prms.tolist(), fil)

    # Training.
    tim_srt_s = time.time()
    net = train(
        net, args["epochs"], ldr_trn, ldr_val, dev, args["early_stop"],
        args["val_patience"], out_flp, args["learning_rate"], args["momentum"],
        args["val_improvement_thresh"], args["timeout_s"])
    print(f"Finished training - time: {time.time() - tim_srt_s:.2f} seconds")

    # # Read the best version of the model from disk.
    # net = torch.jit.load(out_flp)
    # net.to(dev)

    # Testing.
    tim_srt_s = time.time()
    los_tst = test(net, ldr_tst, dev)
    print(f"Finished testing - time: {time.time() - tim_srt_s:.2f} seconds")
    return los_tst


def run_many(args):
    """
    Run args["conf_trials"] trials and survive args["max_attempts"] failed
    attempts.
    """
    # TODO: Parallelize attempts.
    trls = args["conf_trials"]
    apts = 0
    apts_max = args["max_attempts"]
    ress = []
    while trls > 0 and apts < apts_max:
        apts += 1
        res = run(args)
        if res == 100:
            print(
                (f"Training failed (attempt {apts}/{apts_max}). Trying again!"))
        else:
            ress.append(res)
            trls -= 1
    if ress:
        print(("Resulting accuracies: "
               f"{', '.join([f'{res:.2f}%' for res in ress])}"))
        max_acc = max(ress)
        print(f"Maximum accuracy: {max_acc:.2f}%")
        # Return the minimum error instead of the maximum accuracy.
        return 1 - max_acc
    print(f"Model cannot be trained with args: {args}")
    return float("inf")


def main():
    """ This program's entrypoint. """
    # Parse command line arguments.
    psr = argparse.ArgumentParser(description="An LSTM training framework.")
    psr.add_argument(
        "--data-dir",
        help=("The path to a directory containing the"
              "training/validation/testing data (required)."),
        required=True, type=str)
    model_opts = sorted(models.MODELS.keys())
    psr.add_argument(
        "--model", default=model_opts[0], help="The model to use.",
        choices=model_opts, type=str)
    psr.add_argument(
        "--epochs", default=DEFAULTS["epochs"],
        help="The number of epochs to train for.", type=int)
    psr.add_argument(
        "--num-gpus", default=DEFAULTS["num_gpus"],
        help="The number of GPUs to use.", type=int)
    psr.add_argument(
        "--train-batch", default=DEFAULTS["train_batch"],
        help="The batch size to use during training.", type=int)
    psr.add_argument(
        "--test-batch", default=DEFAULTS["test_batch"],
        help="The batch size to use during validation and testing.", type=int)
    psr.add_argument(
        "--learning-rate", default=DEFAULTS["learning_rate"],
        help="Learning rate for SGD training.", type=float)
    psr.add_argument(
        "--momentum", default=DEFAULTS["momentum"],
        help="Momentum for SGD training.", type=float)
    psr.add_argument(
        "--early-stop", action="store_true", help="Enable early stopping.")
    psr.add_argument(
        "--val-patience", default=DEFAULTS["val_patience"],
        help=("The number of times that the validation loss can increase "
              "before training is automatically aborted."), type=int)
    psr.add_argument(
        "--val-improvement-thresh", default=DEFAULTS["val_improvement_thresh"],
        help="Threshold for percept improvement in validation loss.",
        type=float)
    psr.add_argument(
        "--conf-trials", default=DEFAULTS["conf_trials"],
        help="The number of trials to run.", type=int)
    psr.add_argument(
        "--max-attempts", default=DEFAULTS["max_attempts"],
        help="The maximum number of failed training attempts to survive.",
        type=int)
    psr.add_argument(
        "--no-rand", action="store_true", help="Use a fixed random seed.")
    psr.add_argument(
        "--timeout-s", default=DEFAULTS["timeout_s"],
        help="Automatically stop training after this amount of time (seconds).",
        type=float)
    psr.add_argument(
        "--out-dir", default=DEFAULTS["out_dir"],
        help="The directory in which to store output files.", type=str)

    run_many(vars(psr.parse_args()))


if __name__ == "__main__":
    main()