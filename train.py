import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.optim import Adam
from torch.cuda.amp import GradScaler
import gc
from tqdm.auto import tqdm
from pathlib import Path
import argparse
from time import time

import config
from pretrain.wordpiece import load_bert_tokenizer
from pretrain.bookcorpus import BookCorpusForBERT
from model import BERTBaseForPretraining
from masked_language_model import MaskedLanguageModel
from pretrain.loss import PretrainingLoss
from utils import get_elapsed_time


def get_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--epubtxt_dir", type=str, required=False, default="../bookcurpus/epubtxt",
    )
    parser.add_argument("--batch_size", type=int, required=False, default=256)

    args = parser.parse_args()
    return args


def save_checkpoint(step, model, optim, scaler, ckpt_path):
    Path(ckpt_path).parent.mkdir(parents=True, exist_ok=True)
    ckpt = {
        "step": step,
        "optimizer": optim.state_dict(),
        "scaler": scaler.state_dict(),
    }
    if config.N_GPUS > 1:
        ckpt["model"] = model.module.state_dict()
    else:
        ckpt["model"] = model.state_dict()
    torch.save(ckpt, str(ckpt_path))
    print(f"""Saved checkpoint.""")


if __name__ == "__main__":
    # torch.autograd.set_detect_anomaly(True)

    gc.collect()
    torch.cuda.empty_cache()

    args = get_args()

    # "We train with batch size of 256 sequences (256 sequences * 512 tokens
    # = 128,000 tokens/batch) for 1,000,000 steps, which is approximately 40 epochs
    # over the 3.3 billion word corpus." (Comment: 256 * 512 * 1,000,000 / 3,300,000,000
    # = 39.7)
    N_STEPS = (256 * 512 * 1_000_000) // (args.batch_size * config.MAX_LEN)
    print(f"""BATCH_SIZE = {args.batch_size}""")
    print(f"""MAX_LEN = {config.MAX_LEN}""")
    print(f"""N_STEPS = {N_STEPS:,}""", end="\n\n")

    tokenizer = load_bert_tokenizer(config.VOCAB_PATH)
    ds = BookCorpusForBERT(
        epubtxt_dir=args.epubtxt_dir,
        tokenizer=tokenizer,
        max_len=config.MAX_LEN,
    )
    dl = DataLoader(
        ds,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=config.N_WORKERS,
        pin_memory=True,
        drop_last=True,
    )
    di = iter(dl)

    model = BERTBaseForPretraining(vocab_size=config.VOCAB_SIZE).to(config.DEVICE)
    if config.N_GPUS > 1:
        model = nn.DataParallel(model)
    mlm = MaskedLanguageModel(
        vocab_size=config.VOCAB_SIZE,
        mask_id=tokenizer.token_to_id("[MASK]"),
        select_prob=config.SELECT_PROB,
        mask_prob=config.MASK_PROB,
        randomize_prob=config.RANDOMIZE_PROB,
    )

    optim = Adam(
        model.parameters(),
        lr=config.MAX_LR,
        betas=(config.BETA1, config.BETA2),
        weight_decay=config.WEIGHT_DECAY,
    )

    scaler = GradScaler(enabled=True if config.AUTOCAST else False)

    crit = PretrainingLoss()

    ### Resume
    if config.CKPT_PATH is not None:
        ckpt = torch.load(config.CKPT_PATH, map_location=config.DEVICE)
        if config.N_GPUS > 1:
            model.module.load_state_dict(ckpt["model"])
        else:
            model.load_state_dict(ckpt["model"])
        optim.load_state_dict(ckpt["optimizer"])
        scaler.load_state_dict(ckpt["scaler"])
        init_step = ckpt["step"]
        prev_ckpt_path = config.CKPT_PATH
    else:
        init_step = 0
        prev_ckpt_path = ".pth"

    print("Training...")
    running_nsp_loss = 0
    running_mlm_loss = 0
    step_cnt = 0
    start_time = time()
    for step in range(init_step + 1, N_STEPS + 1):
        try:
            token_ids, seg_ids, is_next = next(di)
        except StopIteration:
            di = iter(dl)
            token_ids, seg_ids, is_next = next(di)

        token_ids = token_ids.to(config.DEVICE)
        seg_ids = seg_ids.to(config.DEVICE)
        is_next = is_next.to(config.DEVICE)

        token_ids = mlm(token_ids)

        with torch.autocast(
            device_type=config.DEVICE.type,
            dtype=torch.float16,
            enabled=True if config.AUTOCAST else False,
        ):
            nsp_pred, mlm_pred = model(seq=token_ids, seg_ids=seg_ids)
            # loss = crit(
            nsp_loss, mlm_loss = crit(
                mlm_pred=mlm_pred, nsp_pred=nsp_pred, token_ids=token_ids, is_next=is_next,
            )
            loss = nsp_loss + mlm_loss
        optim.zero_grad()
        if config.AUTOCAST:
            scaler.scale(loss).backward()
            scaler.step(optim)
            scaler.update()
        else:
            loss.backward()
            optim.step()

        running_nsp_loss += nsp_loss.item()
        running_mlm_loss += mlm_loss.item()
        step_cnt += 1

        if (step % (config.N_CKPT_SAMPLES // args.batch_size) == 0) or (step == N_STEPS):
            print(f"""[ {step:,}/{N_STEPS:,} ][ {get_elapsed_time(start_time)} ]""", end="")
            print(f"""[ NSP loss: {running_nsp_loss / step_cnt:.3f} ]""", end="")
            print(f"""[ MLM loss: {running_mlm_loss / step_cnt:.3f} ]""")

            running_loss = 0
            step_cnt = 0

            cur_ckpt_path = config.CKPT_DIR/f"""bookcorpus_step_{step}.pth"""
            save_checkpoint(
                step=step, model=model, optim=optim, scaler=scaler, ckpt_path=cur_ckpt_path,
            )
            if Path(prev_ckpt_path).exists():
                prev_ckpt_path.unlink()
            prev_ckpt_path = cur_ckpt_path
