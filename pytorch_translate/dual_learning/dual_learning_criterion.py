#!/usr/bin/env python3

import math

import torch
from fairseq import bleu, utils
from fairseq.criterions import FairseqCriterion, register_criterion
from pytorch_translate import beam_decode
from pytorch_translate.data.weighted_data import WeightedLanguagePairDataset


@register_criterion("unsupervised_criterion")
class UnsupervisedCriterion(FairseqCriterion):
    """This criterion computes losses from input (monolingual data in
    translation) with two components:
    1. Reconstruction loss:
    2. LM loss:
    The total loss is a weighted sum of these two components.
    """

    def __init__(self, args, task):
        super().__init__(args, task)
        self.args = args
        self.alpha = args.reward_alpha
        self.remove_eos_at_src = not args.append_eos_to_source
        self.task = task

    def _generate_translation(self, model, tgt_dict, sample, **kwargs):
        translator_class = beam_decode.SequenceGenerator
        translator = translator_class(models=[model], tgt_dict=tgt_dict, **kwargs)
        translator.cuda()
        s = utils.move_to_cuda(sample)

        # TODO: nbest
        input = s["net_input"]
        srclen = input["src_tokens"].size(1)
        if self.task.use_char_source:
            encoder_input = {
                k: v
                for k, v in input.items()
                if k in ["src_tokens", "src_lengths", "char_inds", "word_lengths"]
            }
        else:
            encoder_input = {
                k: v for k, v in input.items() if k in ["src_tokens", "src_lengths"]
            }
        with torch.no_grad():
            hypos = translator.generate(
                encoder_input=encoder_input,
                beam_size=self.args.beam,
                maxlen=int(self.args.max_len_a * srclen + self.args.max_len_b),
            )
            for i, id in enumerate(s["id"]):
                # remove padding
                src = utils.strip_pad(input["src_tokens"][i, :], tgt_dict.pad())
                yield id, src, hypos[i]

    def forward(
        self,
        sample,
        forward_model,
        forward_optimizer,
        tgt_dict,
        backward_model,
        backward_optimizer,
        src_dict,
        lm_scorer=None,
        reduce=True,
        **generate_kwargs,
    ):
        """Compute the reconstruction and LM loss from forward and backward
        models.

        Args:
            sample: original input.
            hypos: psudo labels generated by the forward model. They are used
                as approximation of the target space to do importance sampling.
            forward_model: the model used to generate psuedo labels.
            backward_model: the model to reconstruct original input using
                psuedo labels.
            lm_scorer: an LM model eval mode to score psuedo labels in target
                space.
        """
        # Generate translations
        nbest_translations = self._generate_translation(
            forward_model, tgt_dict, sample, **generate_kwargs
        )

        forward_samples = []
        backward_samples = []
        # TODO (T36875783): load pretrained lm to score
        lm_score = 0.5
        eos_index = tgt_dict.eos()
        # compute each model's reward
        forward_reward = lm_score
        for id, src, hypos in nbest_translations:
            # construct the sample; compute the ce loss
            # backward_samples need to handle EOS
            original_src = src
            bt_src = hypos[0]["tokens"]
            # add EOS to the target, i.e. original source, since it'll be used
            # as target
            if original_src[-1] != eos_index:
                original_src = torch.cat(
                    [original_src.cpu(), torch.LongTensor([eos_index])]
                )
            # remove EOS in the src is optional
            if self.remove_eos_at_src:
                bt_src = bt_src[:-1]
            backward_sample = {
                "id": id,
                "source": bt_src.cpu(),  # first hypo is best hypo
                "target": original_src.cpu(),
                "weights": 1.0 - self.alpha,
            }
            backward_samples.append(backward_sample)
            # use bleu score as reward
            bwd_model_input = utils.move_to_cuda(
                WeightedLanguagePairDataset.collate(
                    samples=[backward_sample],
                    pad_idx=src_dict.pad(),
                    eos_idx=src_dict.eos(),
                )
            )
            reconstructed_source = self._generate_translation(
                backward_model, src_dict, bwd_model_input, **generate_kwargs
            )
            scorer = bleu.Scorer(src_dict.pad(), src_dict.eos(), src_dict.unk())
            for _, _, x_hypos in reconstructed_source:
                x_hat = x_hypos[0]["tokens"][:-1]
                scorer.add(original_src.int(), x_hat.int().cpu())
            backward_reward = scorer.score(order=4) / 100.0

            total_reward = (
                self.alpha * forward_reward + (1.0 - self.alpha) * backward_reward
            )

            assert hypos[0]["tokens"][-1] == eos_index, (
                f"Expected generated translation to have eos (id: "
                f"{eos_index}) at end, but instead found token id "
                f"{hypos[0]['tokens'][-1]} at end."
            )
            forward_samples.append(
                {
                    "id": id,
                    "source": src.cpu(),
                    "target": hypos[0]["tokens"].cpu(),  # first hypo is best hypo
                    "weights": total_reward,
                }
            )

        # Now combine pseudo labelled examples to corresponding batch with
        # rewards factored to weighting of each task's loss
        agg_loss, agg_sample_size, agg_logging_output = 0.0, 0.0, {}
        forward_model.train()
        forward_loss, sample_size, logging_output = self.task.criterion(
            forward_model,
            utils.move_to_cuda(
                WeightedLanguagePairDataset.collate(
                    samples=forward_samples,
                    pad_idx=tgt_dict.pad(),
                    eos_idx=tgt_dict.eos(),
                )
            ),
        )
        agg_loss += forward_loss.detach().item()
        agg_sample_size += sample_size
        agg_logging_output["primal"] = logging_output
        # grad would be further scaled when passed back to trainer,
        # which will do the update
        forward_optimizer.backward(forward_loss)

        backward_model.train()
        backward_loss, sample_size, logging_output = self.task.criterion(
            backward_model,
            utils.move_to_cuda(
                WeightedLanguagePairDataset.collate(
                    samples=backward_samples,
                    pad_idx=src_dict.pad(),
                    eos_idx=src_dict.eos(),
                )
            ),
        )

        agg_loss += backward_loss.data.item()
        agg_sample_size += sample_size
        agg_logging_output["dual"] = logging_output
        backward_optimizer.backward(backward_loss)
        return agg_loss, agg_sample_size, agg_logging_output

    @staticmethod
    def aggregate_logging_outputs(logging_outputs):
        """Aggregate logging outputs from data parallel training."""

        def get_logging_output(key):
            if key in logging_outputs[0].keys():
                return logging_outputs[0][key]
            else:
                return sum(
                    log[key] if key in log else 0
                    for _, log in logging_outputs[0].items()
                )

        loss_sum = get_logging_output("loss")
        ntokens = get_logging_output("ntokens")
        nsentences = get_logging_output("nsentences")
        sample_size = get_logging_output("sample_size")
        agg_output = {
            "loss": loss_sum / sample_size / math.log(2),
            "ntokens": ntokens,
            "nsentences": nsentences,
            "sample_size": sample_size,
        }
        if sample_size != ntokens:
            agg_output["nll_loss"] = loss_sum / ntokens / math.log(2)
        return agg_output
