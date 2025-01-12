import torch
import torch.nn as nn
import torch.nn.functional as F
from pytorch_lightning import LightningModule
from pytorch_lightning.loggers.csv_logs import CSVLogger
import torch_geometric as pyg
from torch.nn import CrossEntropyLoss
from torchmetrics.classification import BinaryF1Score, BinaryAccuracy, MulticlassAccuracy
import numpy as np

from musicparser.metrics import CTreeSpanSimilarity, VariableMulticlassAccuracy, ArcsAccuracy, CTreeNodeSimilarity
from musicparser.rpr import TransformerEncoderLayerRPR, TransformerEncoderRPR, DummyDecoder
from musicparser.postprocessing import chuliu_edmonds_one_root, dtree2unlabeled_ctree, eisner, eisner_fast
from musicparser.data_loading import DURATIONS, get_feats_one_hot, METRICAL_LEVELS, NUMBER_OF_PITCHES, CHORD_FORM, CHORD_EXTENSION, JTB_DURATION, get_head_seq


class ArcPredictionLightModel(LightningModule):
    def __init__(
        self,
        in_feats,
        n_hidden,
        n_layers=2,
        activation="relu",
        dropout=0.3,
        lr=0.001,
        weight_decay=5e-4,
        pos_weight = None,
        embedding_dim = {"pitch": 24, "duration": 6, "metrical": 2},
        use_embeddings = True,
        biaffine = False,
        encoder_type = "rnn",
        n_heads = 4,
        data_type = "notes",
        rpr = False,
        pretrain_mode = False,
        loss_type = 'ce',
        optimizer = 'adamw',
        warmup_steps = 10,
        max_epochs = 100,
        len_train_dataloader = 100,
    ):
        super().__init__()
        self.lr = lr
        self.weight_decay = weight_decay
        self.save_hyperparameters()
        self.module = ArcPredictionModel(
            in_feats,
            n_hidden,
            n_layers,
            activation,
            dropout,
            embedding_dim,
            use_embeddings,
            biaffine,
            encoder_type,
            n_heads,
            data_type,
            rpr,
            pretrain_mode,
        )
        pos_weight = 1 if pos_weight is None else pos_weight
        self.data_type = data_type
        self.optimizer = optimizer
        self.warmup_steps = warmup_steps
        self.len_train_dataloader = len_train_dataloader
        self.max_epochs = max_epochs
        self.loss_type = loss_type
        if loss_type == 'bce':
            self.train_loss = torch.nn.BCEWithLogitsLoss(pos_weight= torch.tensor([pos_weight]))
            self.val_loss = torch.nn.BCEWithLogitsLoss(pos_weight= torch.tensor([pos_weight]))
        elif loss_type == 'ce':
            self.train_loss = torch.nn.CrossEntropyLoss(ignore_index=-1) 
            self.val_loss = torch.nn.CrossEntropyLoss(ignore_index=-1) 
        elif loss_type == 'both':
            self.train_loss_bce = torch.nn.BCEWithLogitsLoss(pos_weight= torch.tensor([pos_weight]))
            self.val_loss_bce = torch.nn.BCEWithLogitsLoss(pos_weight= torch.tensor([pos_weight]))
            self.train_loss_ce = torch.nn.CrossEntropyLoss(ignore_index=-1) 
            self.val_loss_ce = torch.nn.CrossEntropyLoss(ignore_index=-1) 
        else:
            raise ValueError(f"loss_type {loss_type} not supported")
        self.val_f1score = BinaryF1Score()
        self.val_f1score_postp = BinaryF1Score()
        self.val_head_accuracy = VariableMulticlassAccuracy()
        self.val_head_accuracy_postp = VariableMulticlassAccuracy()
        self.val_arc_accuracy_postp = ArcsAccuracy()
        self.val_span_similarity = CTreeSpanSimilarity()
        self.val_node_similarity = CTreeNodeSimilarity()
        self.test_f1score = BinaryF1Score()
        self.test_f1score_postp = BinaryF1Score()
        self.test_head_accuracy = VariableMulticlassAccuracy(ignore_index=-1)
        self.test_head_accuracy_postp = VariableMulticlassAccuracy(ignore_index=-1)
        self.test_arc_accuracy_postp = ArcsAccuracy()
        self.test_span_similarity = CTreeSpanSimilarity()
        self.test_node_similarity = CTreeNodeSimilarity()
        self.pretrain_mode = pretrain_mode
        if pretrain_mode:
            # self.pre_train_loss = nn.ModuleDict({"root": CrossEntropyLoss(), "form": CrossEntropyLoss(), "ext": CrossEntropyLoss(), "dur": CrossEntropyLoss(), "met": CrossEntropyLoss()})
            self.pre_train_loss = CrossEntropyLoss()
            self.pre_train_accuracy = nn.ModuleDict({"root": MulticlassAccuracy(12), "form": MulticlassAccuracy(len(CHORD_FORM)), "ext": MulticlassAccuracy(len(CHORD_EXTENSION)), "dur": MulticlassAccuracy(len(JTB_DURATION)), "met": MulticlassAccuracy(METRICAL_LEVELS)})
            # self.pre_val_loss = nn.ModuleDict({"root": CrossEntropyLoss(), "form": CrossEntropyLoss(), "ext": CrossEntropyLoss(), "dur": CrossEntropyLoss(), "met": CrossEntropyLoss()})
            self.pre_val_loss = CrossEntropyLoss()
            self.pre_val_accuracy = nn.ModuleDict({"root": MulticlassAccuracy(12), "form": MulticlassAccuracy(len(CHORD_FORM)), "ext": MulticlassAccuracy(len(CHORD_EXTENSION)), "dur": MulticlassAccuracy(len(JTB_DURATION)), "met": MulticlassAccuracy(METRICAL_LEVELS)})

    def training_step(self, batch, batch_idx):
        note_seq, truth_arcs_mask, pot_arcs, head_seqs = batch
        note_seq, truth_arcs_mask, pot_arcs, head_seqs = note_seq[0], truth_arcs_mask[0], pot_arcs[0], head_seqs[0]
        if not self.pretrain_mode: # normal mode, predict arcs
            arc_pred_mask_logits = self.module(note_seq, pot_arcs)
            if self.loss_type == 'bce':
                loss = self.train_loss(arc_pred_mask_logits.float(), truth_arcs_mask.float())
            elif self.loss_type == 'ce':
                num_notes = len(note_seq)
                # add the extra line and row for the root node
                adj_pred_logits_root = self.compute_adj_logits_root(pot_arcs, arc_pred_mask_logits, num_notes)
                loss = self.train_loss(adj_pred_logits_root.T,head_seqs.long())
            elif self.loss_type == 'both':
                num_notes = len(note_seq)
                # add the extra line and row for the root node
                adj_pred_logits_root = self.compute_adj_logits_root(pot_arcs, arc_pred_mask_logits, num_notes)
                loss_bce = self.train_loss_bce(arc_pred_mask_logits.float(), truth_arcs_mask.float())
                loss_ce = self.train_loss_ce(adj_pred_logits_root.T,head_seqs.long())
                loss = loss_bce + loss_ce
            self.log("train_loss", loss.item(), prog_bar=True, on_step=True, on_epoch=True, batch_size=1)
            return loss
        else: # pretrain mode, predict chord labels
            # shift input sequence to the right, and shorten prediction by one, to compare with prediction at next position
            input = note_seq[:-1,:]
            expected = note_seq[1:,:]
            # get mask for input sequence
            mask = generate_square_subsequent_mask(len(expected)).to(self.device)
            # predict chord labels
            pred_logits = self.module(input,None,mask=mask)
            loss = 0
            accuracy = 0
            for i,key in enumerate(pred_logits.keys()):
                loss+= self.pre_train_loss(pred_logits[key], expected[:,i].long())
                # self.log(f"train_loss_{key}", loss.item(), prog_bar=True, on_step=True, on_epoch=True, batch_size=1)
                accuracy += self.pre_train_accuracy[key](pred_logits[key], expected[:,i].long())
                # self.log(f"train_acc_{key}", acc.item(), prog_bar=True, on_step=True, on_epoch=True, batch_size=1)
            # loss = loss/len(pred_logits.keys())
            accuracy = accuracy/len(pred_logits.keys())
            self.log("pre_train_loss", loss.item(), prog_bar=True, on_step=True, on_epoch=True, batch_size=1)
            self.log("pre_train_acc", accuracy.item(), prog_bar=True, on_step=True, on_epoch=True, batch_size=1)
            return loss


    def validation_step(self, batch, batch_idx):
        note_seq, truth_arcs_mask, pot_arcs, head_seqs = batch
        note_seq, truth_arcs_mask, pot_arcs, head_seqs = note_seq[0], truth_arcs_mask[0], pot_arcs[0], head_seqs[0]
        if not self.pretrain_mode: # normal mode, predict arcs
            num_notes = len(note_seq)
            # predict arcs
            arc_pred_mask_logits = self.module(note_seq, pot_arcs)
            arc_pred__mask_normalized = torch.sigmoid(arc_pred_mask_logits)
            pred_arc = pot_arcs[torch.round(arc_pred__mask_normalized).squeeze().bool()]
            truth_arc = pot_arcs[truth_arcs_mask.bool()]
            # predict rest mask
            if self.data_type == "notes":
                # find rests
                is_rest = head_seqs == -1
            else:    
                is_rest = torch.zeros_like(head_seqs).bool() # for chords there are no rests
            # compute adjency matrix of logits predictions
            adj_pred_logits_root = self.compute_adj_logits_root(pot_arcs, arc_pred_mask_logits, num_notes)
            # compute loss
            if self.loss_type == 'bce':
                val_loss = self.val_loss(arc_pred_mask_logits.float(), truth_arcs_mask.float())
            elif self.loss_type == 'ce':
                val_loss = self.val_loss(adj_pred_logits_root.T,head_seqs.long())
            elif self.loss_type == 'both':
                val_loss_bce = self.val_loss_bce(arc_pred_mask_logits.float(), truth_arcs_mask.float())
                val_loss_ce = self.val_loss_ce(adj_pred_logits_root.T,head_seqs.long())
                val_loss = val_loss_bce + val_loss_ce
            self.log("val_loss", val_loss.item(), on_epoch=True, batch_size=1)
            # compute binary F1 score and accuracy
            # adj_pred = self.pred_dlist2adj(pred_arc,num_notes)
            adj_pred = (adj_pred_logits_root > 0).long().cpu()
            adj_target = self.compute_adj_root(truth_arc,num_notes).long().cpu()
            val_fscore = self.val_f1score.cpu()(adj_pred.flatten(), adj_target.flatten())
            self.log("val_fscore", val_fscore.item(), prog_bar=False, batch_size=1)
            val_head_accuracy = self.val_head_accuracy(torch.argmax(adj_pred_logits_root, dim =0), head_seqs.long())
            self.log("val_head_accuracy", val_head_accuracy.item(), prog_bar=True, batch_size=1)
            # postprocess
            adj_pred_postp, pred_arc_postp, head_seqs_postp = self.postprocess(adj_pred_logits_root, num_notes, is_rest)
            # compute postprocessed F1 score
            val_fscore_postp = self.val_f1score_postp.cpu()(adj_pred_postp.flatten().cpu(), adj_target.flatten())
            self.log("val_fscore_postp", val_fscore_postp.item(), prog_bar=False, batch_size=1)
            # compute head accuracy
            # head_seqs_postp = get_head_seq(pred_arc_postp, num_notes, check_unique_root=False) 
            val_head_accuracy_postp = self.val_head_accuracy_postp.cpu()(head_seqs_postp.long(), head_seqs.long().cpu())
            self.log("val_head_accuracy_postp", val_head_accuracy_postp.item(), prog_bar=True, batch_size=1)
            # compute arcs accuracy
            rootless_pred_arc_postp = pred_arc_postp[pred_arc_postp[:,0]!=0]
            rootless_truth_arc = truth_arc[truth_arc[:,0]!=0]
            val_arc_accuracy_postp = self.val_arc_accuracy_postp.cpu()(rootless_pred_arc_postp.cpu(), rootless_truth_arc.cpu())
            self.log("val_arc_accuracy_postp", val_arc_accuracy_postp.item(), prog_bar=True, batch_size=1)
            # compute c_tree span and node similarity
            pred_ctree = dtree2unlabeled_ctree(pred_arc_postp.cpu())
            truth_ctree = dtree2unlabeled_ctree(truth_arc.cpu())
            val_span_sim = self.val_span_similarity.cpu()(pred_ctree, truth_ctree)
            self.log("val_ctree_sim", val_span_sim.item(), prog_bar=True, batch_size=1)
            val_node_sim = self.val_node_similarity.cpu()(pred_ctree, truth_ctree)
            self.log("val_node_sim", val_node_sim.item(), prog_bar=True, batch_size=1)
            # log other useful stuff for debugging
            # if not isinstance(self.logger, CSVLogger):
            #     self.logger.log_text(key="head_seqs", columns = ["head_seqs","head_seqs_postp","truth_head_seqs" ], data= [[str(torch.argmax(adj_pred_logits_root, dim =0).tolist()), str(head_seqs_postp.tolist()),str(head_seqs.long().tolist())]])
            #     self.logger.log_text(key="ctrees", columns = ["pred_ctree","truth_ctree"], data= [[str(pred_ctree.unlabeled_repr()),str(truth_ctree.unlabeled_repr())]])
        else:
            # shift input sequence to the right, and shorten prediction by one, to compare with prediction at next position
            input = note_seq[:-1,:]
            expected = note_seq[1:,:]
            # get mask for input sequence
            mask = generate_square_subsequent_mask(len(expected)).to(self.device)
            # predict chord labels
            pred_logits = self.module(input,None,mask=mask)
            loss = 0
            accuracy = 0
            for i,key in enumerate(pred_logits.keys()):
                loss+= self.pre_train_loss(pred_logits[key], expected[:,i].long())
                # self.log(f"train_loss_{key}", loss.item(), prog_bar=True, on_step=True, on_epoch=True, batch_size=1)
                accuracy += self.pre_train_accuracy[key](pred_logits[key], expected[:,i].long())
                # self.log(f"train_acc_{key}", acc.item(), prog_bar=True, on_step=True, on_epoch=True, batch_size=1)
            # loss = loss/len(pred_logits.keys())
            accuracy = accuracy/len(pred_logits.keys())
            self.log("pre_val_loss", loss.item(), prog_bar=True, on_epoch=True, batch_size=1)
            self.log("pre_val_acc", accuracy.item(), prog_bar=True, on_epoch=True, batch_size=1)

    
    def test_step(self, batch, batch_idx):
        note_seq, truth_arcs_mask, pot_arcs, head_seqs = batch
        note_seq, truth_arcs_mask, pot_arcs, head_seqs = note_seq[0], truth_arcs_mask[0], pot_arcs[0], head_seqs[0]
        num_notes = len(note_seq)
        # predict rest mask
        if self.data_type == "notes":
            # find rests
            is_rest = head_seqs == -1
        else:    
            is_rest = torch.zeros_like(head_seqs).bool() # for chords there are no rests
        # predict arcs
        arc_pred_mask_logits = self.module(note_seq, pot_arcs)
        arc_pred__mask_normalized = torch.sigmoid(arc_pred_mask_logits)
        pred_arc = pot_arcs[torch.round(arc_pred__mask_normalized).squeeze().bool()]
        truth_arc = pot_arcs[truth_arcs_mask.bool()]
        adj_pred_logits_root = self.compute_adj_logits_root(pot_arcs, arc_pred_mask_logits, num_notes)
        # compute binary F1 score and accuracy
        adj_pred = (adj_pred_logits_root > 0).long().cpu()
        adj_target = self.compute_adj_root(truth_arc,num_notes).long().cpu()
        test_fscore = self.test_f1score.cpu()(adj_pred.flatten(), adj_target.flatten())
        self.log("test_fscore", test_fscore.item(), prog_bar=False, batch_size=1)
        test_head_accuracy = self.test_head_accuracy(torch.argmax(adj_pred_logits_root, dim =0), head_seqs.long())
        self.log("test_head_accuracy", test_head_accuracy.item(), prog_bar=True, batch_size=1)
        # postprocess
        adj_pred_postp, pred_arc_postp, head_seqs_postp = self.postprocess(adj_pred_logits_root, num_notes, is_rest)
        # compute postprocessed F1 score
        test_fscore_postp = self.test_f1score_postp.cpu()(adj_pred_postp.flatten().cpu(), adj_target.flatten())
        self.log("test_fscore_postp", test_fscore_postp.item(), prog_bar=False, batch_size=1)
        # compute head accuracy
        # head_seqs_postp = get_head_seq(pred_arc_postp, num_notes,check_unique_root=False) 
        test_head_accuracy_postp = self.test_head_accuracy_postp.cpu()(head_seqs_postp.long(), head_seqs.long().cpu())
        self.log("test_head_accuracy_postp", test_head_accuracy_postp.item(), prog_bar=True, batch_size=1)
        # compute arcs accuracy
        rootless_pred_arc_postp = pred_arc_postp[pred_arc_postp[:,0]!=0]
        rootless_truth_arc = truth_arc[truth_arc[:,0]!=0]
        test_arc_accuracy_postp = self.test_arc_accuracy_postp.cpu()(rootless_pred_arc_postp.cpu(), rootless_truth_arc.cpu())
        self.log("test_arc_accuracy_postp", test_arc_accuracy_postp.item(), prog_bar=True, batch_size=1)
        # compute c_tree span and node similarity
        pred_ctree = dtree2unlabeled_ctree(pred_arc_postp.cpu())
        truth_ctree = dtree2unlabeled_ctree(truth_arc.cpu())
        test_span_sim = self.test_span_similarity.cpu()(pred_ctree, truth_ctree)
        self.log("test_ctree_sim", test_span_sim.item(), prog_bar=True, batch_size=1)
        test_node_sim = self.test_node_similarity.cpu()(pred_ctree, truth_ctree)
        self.log("test_node_sim", test_node_sim.item(), prog_bar=True, batch_size=1)
        if not isinstance(self.logger, CSVLogger):
            self.logger.log_text(key="test_head_seqs", columns = ["head_seqs","head_seqs_postp","truth_head_seqs" ], data= [[str(torch.argmax(adj_pred_logits_root, dim =0).tolist()), str(head_seqs_postp.tolist()),str(head_seqs.long().tolist())]])
            self.logger.log_text(key="test_ctrees", columns = ["pred_ctree","truth_ctree"], data= [[str(pred_ctree.unlabeled_repr()),str(truth_ctree.unlabeled_repr())]])

    
    def predict_step(self, batch, batch_idx):
        note_seq, truth_arcs_mask, pot_arcs, head_seqs = batch
        note_seq, truth_arcs_mask, pot_arcs, head_seqs = note_seq[0], truth_arcs_mask[0], pot_arcs[0], head_seqs[0]
        num_notes = len(note_seq)
        # predict arcs
        arc_pred_mask_logits = self.module(note_seq, pot_arcs)
        arc_pred__mask_normalized = torch.sigmoid(arc_pred_mask_logits)
        pred_arc = pot_arcs[torch.round(arc_pred__mask_normalized).squeeze().bool()]
        truth_arc = pot_arcs[truth_arcs_mask.bool()]
        adj_pred_logits_root = self.compute_adj_logits_root(pot_arcs, arc_pred_mask_logits, num_notes)
        # compute binary F1 score and accuracy
        adj_pred = (adj_pred_logits_root > 0).long().cpu()
        adj_target = self.compute_adj_root(truth_arc,num_notes).long().cpu()
        test_fscore = self.test_f1score.cpu()(adj_pred.flatten(), adj_target.flatten())
        print("test_fscore", test_fscore.item())
        test_head_accuracy = self.test_head_accuracy(torch.argmax(adj_pred_logits_root, dim =0), head_seqs.long())
        print("test_head_accuracy", test_head_accuracy.item())
        # postprocess
        adj_pred_postp, pred_arc_postp, head_seqs_postp = self.postprocess(adj_pred_logits_root, num_notes)
        # compute postprocessed F1 score
        test_fscore_postp = self.test_f1score_postp.cpu()(adj_pred_postp.flatten().cpu(), adj_target.flatten())
        print("test_fscore_postp", test_fscore_postp.item())
        # compute head accuracy
        # head_seqs_postp = get_head_seq(pred_arc_postp, num_notes,check_unique_root=False) 
        test_head_accuracy_postp = self.test_head_accuracy_postp.cpu()(head_seqs_postp.long(), head_seqs.long().cpu())
        print("test_head_accuracy_postp", test_head_accuracy_postp.item())
        # compute arcs accuracy
        rootless_pred_arc_postp = pred_arc_postp[pred_arc_postp[:,0]!=0]
        rootless_truth_arc = truth_arc[truth_arc[:,0]!=0]
        test_arc_accuracy_postp = self.test_arc_accuracy_postp.cpu()(rootless_pred_arc_postp.cpu(), rootless_truth_arc.cpu())
        print("test_arc_accuracy_postp", test_arc_accuracy_postp.item())
        # compute c_tree span and node similarity
        pred_ctree = dtree2unlabeled_ctree(pred_arc_postp.cpu())
        truth_ctree = dtree2unlabeled_ctree(truth_arc.cpu())
        test_span_sim = self.test_span_similarity.cpu()(pred_ctree, truth_ctree)
        print("test_ctree_sim", test_span_sim.item())
        test_node_sim = self.test_node_similarity.cpu()(pred_ctree, truth_ctree)
        print("test_node_sim", test_node_sim.item())
        return {"pot_arcs": pot_arcs, "arc_pred__mask_normalized" : arc_pred__mask_normalized,"head_seq_truth": head_seqs.long().cpu().tolist(),"head_seq_postp" : head_seqs_postp.cpu().tolist(), "head_seq" : torch.argmax(adj_pred_logits_root, dim =0).cpu().tolist() , "pred_arc" : pred_arc.cpu().tolist() , "pred_arc_postp": pred_arc_postp.cpu().tolist(), "truth_arc": truth_arc.cpu().tolist(), "pred_ctree": pred_ctree, "truth_ctree": truth_ctree}

        
    
    def compute_adj_logits_root(self,pot_arcs, arc_pred_mask_logits, num_notes):
        # adj_pred_logits_root = to_dense(torch.sparse_coo_tensor(pot_arcs.T, arc_pred_mask_logits, (num_notes+1, num_notes+1)),fill_value=float("-inf")).to(arc_pred_mask_logits.device)
        adj_pred_logits_root = torch.sparse_coo_tensor(pot_arcs.T, arc_pred_mask_logits, (num_notes+1, num_notes+1),device=arc_pred_mask_logits.device).to_dense()
        adj_pred_logits_root[adj_pred_logits_root==0] = float("-inf") #hoping there are no other elements that are exactly 0
        return adj_pred_logits_root
    
    def compute_adj_root(self,arcs, num_notes):
        return torch.sparse_coo_tensor(arcs.T, torch.ones((len(arcs))), (num_notes+1, num_notes+1),device=arcs.device).to_dense()

 

    def postprocess(self, arc_pred_logits_root, num_notes, is_rest, alg = "eisner"):
        adj_pred_log_probs_root = arc_pred_logits_root[:,~is_rest][~is_rest,:]
        
        if alg == "chuliu_edmonds": #transpose to have an adjency matrix with edges pointing toward the parent node and 
            head_seq = chuliu_edmonds_one_root(adj_pred_log_probs_root.cpu().numpy().T)
        elif alg == "eisner":
            head_seq = eisner(adj_pred_log_probs_root.cpu().numpy())
            if np.sum(head_seq == 0) >1: 
                ###############!!!!!!!!!!!!!!!!!!!!!!!!!! This is a bad trick to avoid the postprocessing having only arcs to 0. 
                # TODO: Solve this problem in the postp algo
                adj_pred_log_probs_root[0,:] = float("-inf")
                adj_pred_log_probs_root[0][0] = 0
                adj_pred_log_probs_root[0][-1] = 0
                head_seq = eisner(adj_pred_log_probs_root.cpu().numpy())
        # elif alg == "eisner_fast":
        #     head_seq = eisner_fast(torch.unsqueeze(adj_pred_log_probs_root,dim=0).cpu().numpy(), torch.ones(1,num_notes))
        else:
            raise ValueError("alg must be either eisner or chuliu_edmonds") 
        
        # reintroduce rests if they exist
        head_seq = np.array(head_seq)
        if torch.sum(is_rest)>0:
            head_seq = reintroduce_rests(head_seq, is_rest.cpu().numpy())
        # check that everything is well formatted, this can be removed for speed
        # assert len(np.unique(head_seq[is_rest.cpu()])) <= 1
        # # assert np.unique(head_seq[is_rest.cpu()])[0] == -1
        # temp_head_seq = head_seq.copy()
        # temp_head_seq[temp_head_seq == -1] =0
        # assert is_rest.cpu()[temp_head_seq].all() == False
        # structure the postprocess results in an adjency matrix with edges that point toward the child node. Also predict the list of d_arcs
        adj_pred_postp = torch.zeros((num_notes+1,num_notes+1), device=self.device)
        pred_arc_postp = []
        for i, head in enumerate(head_seq):
            if i== 0: # we add the self loop (0,0) to adj matrix, but not to the list of arcs
                adj_pred_postp[0, 0] = 1
            elif head < 0: # rest element, we don't add it to the arcs, only to the matrix
                adj_pred_postp[0, i] = 1
            elif head != 0:
                # id is index in note list + 1
                adj_pred_postp[head, i] = 1
                pred_arc_postp.append([head, i])
                assert not is_rest.cpu()[head]
            else: #handle the root. Same as before
                root = i
                adj_pred_postp[head, i] = 1
                pred_arc_postp.append([head, i])
        return adj_pred_postp, torch.tensor(pred_arc_postp, device = self.device), torch.tensor(np.insert(head_seq[1:],0,0))


    def configure_optimizers(self):
        if self.optimizer == "adamw":
            optimizer = torch.optim.AdamW(self.parameters(), lr=self.lr, weight_decay=self.weight_decay)
            self.lr_scheduler = None
        elif self.optimizer == "radam":
            optimizer = torch.optim.RAdam(self.parameters(), lr=self.lr, weight_decay=self.weight_decay)
            self.lr_scheduler = None
        elif self.optimizer == "warmadamw":
            optimizer = torch.optim.AdamW(self.parameters(), lr=self.lr, weight_decay=self.weight_decay)
            self.lr_scheduler = CosineWarmupScheduler(optimizer, self.warmup_steps, self.max_epochs*self.len_train_dataloader)
        elif self.optimizer == "warmadam":
            optimizer = torch.optim.Adam(self.parameters(), lr=self.lr, weight_decay=self.weight_decay)
            self.lr_scheduler = CosineWarmupScheduler(optimizer, self.warmup_steps, self.max_epochs*self.len_train_dataloader)
        else:
            raise ValueError("optimizer must be either warmadamw, or warmadam")
        return { # we don't return scheduler because it has to be apply after each step, not after each epoch
            "optimizer": optimizer,
        }
    
    def optimizer_step(self, *args, **kwargs):
        super().optimizer_step(*args, **kwargs)
        if self.lr_scheduler is not None:
            self.lr_scheduler.step()  # Step per iteration, we need to do it here, not to return the scheduler to the pytorch lightning trainer

def reintroduce_rests(head_seq, is_rest):
    rest_indices = np.where(is_rest)[0]
    new_head_seq = np.zeros_like(is_rest) -1
    new_idx = 0
    # insert the rests in the head_seq
    for i,r in enumerate(is_rest):
        if not r: # not rest
            new_head_seq[i] = head_seq[new_idx]
            new_idx += 1
    # update indices of the heads according to rests
    for i in rest_indices:
            # add 1 to the heads that are after the rest
            new_head_seq[new_head_seq>=i] += 1
    return new_head_seq


class CosineWarmupScheduler(torch.optim.lr_scheduler._LRScheduler):
    def __init__(self, optimizer, warmup, max_iters):
        self.warmup = warmup
        self.max_num_iters = max_iters
        super().__init__(optimizer)

    def get_lr(self):
        lr_factor = self.get_lr_factor(epoch=self.last_epoch)
        return [base_lr * lr_factor for base_lr in self.base_lrs]

    def get_lr_factor(self, epoch):
        # here it says epoch, but we use it as step
        lr_factor = 0.5 * (1 + np.cos(np.pi * epoch / self.max_num_iters))
        if epoch <= self.warmup:
            lr_factor *= epoch * 1.0 / self.warmup
        return lr_factor


class TransformerEncoder(torch.nn.Module):
    def __init__(
        self,
        input_dim,
        hidden_dim,
        encoder_depth,
        n_heads = 4,
        dropout=None,
        activation = "relu",
        rpr = False
    ):
        super().__init__()

        if dropout is None:
            dropout = 0
        self.input_dim = input_dim
        self.rpr = rpr

        self.positional_encoder = PositionalEncoding(
            d_model=input_dim, dropout=dropout, max_len=200
        )
        if not rpr: # normal transformer with absolute positional representation
            encoder_layer = nn.TransformerEncoderLayer(d_model=input_dim, dim_feedforward=hidden_dim, nhead=n_heads, dropout =dropout, activation=activation)
            encoder_norm = nn.LayerNorm(input_dim)
            self.transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers=encoder_depth, norm=encoder_norm)
        else: # relative positional representation
            encoder_norm = nn.LayerNorm(input_dim)
            encoder_layer = TransformerEncoderLayerRPR(input_dim, n_heads, hidden_dim, dropout, activation=activation, er_len=200)
            self.transformer_encoder = TransformerEncoderRPR(encoder_layer, encoder_depth, encoder_norm)

    def forward(self, z, src_mask=None):
        # add positional encoding
        z = self.positional_encoder(z)
        # reshape to (seq_len, batch = 1, input_dim)
        z = torch.unsqueeze(z,dim= 1)
        # run transformer encoder
        z = self.transformer_encoder(src=z, mask=src_mask)
        # remove batch dim
        z = torch.squeeze(z, dim=1)
        return z, ""


class PositionalEncoding(nn.Module):

    def __init__(self, d_model: int, dropout: float = 0.1, max_len: int = 500):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)

        position = torch.arange(max_len).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2) * (-np.log(10000.0) / d_model))
        pe = torch.zeros(max_len, d_model)
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        self.register_buffer('pe', pe)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x: Tensor, shape [seq_len, batch_size, embedding_dim]
        """
        x = x + self.pe[:x.size(0)]
        return self.dropout(x)

class NotesEncoder(torch.nn.Module):
    def __init__(
        self,
        input_dim,
        hidden_dim,
        rnn_depth,
        dropout=0.1,
        embedding_dim = {},
        use_embeddings = True,
        encoder_type = "rnn",
        bidirectional=True,
        activation = "relu",
        n_heads = 4,
        data_type = "notes",
        rpr = False,
    ):
        super().__init__()

        if hidden_dim % 2 != 0:
            raise ValueError("Hidden_dim must be an even integer")
        if use_embeddings and embedding_dim == {}:
            raise ValueError("If use_embeddings is True, embedding_dim must be provided")
        self.hidden_dim = hidden_dim
        self.dropout = nn.Dropout(dropout)
        self.use_embeddings = use_embeddings
        self.data_type = data_type
        self.embedding_dim = embedding_dim

        # Encoder layer
        if encoder_type == "rnn":
            self.encoder_cell = nn.GRU(
                input_size=input_dim,
                hidden_size=hidden_dim // 2 if bidirectional else hidden_dim,
                bidirectional=bidirectional,
                num_layers=rnn_depth,
                dropout=dropout,
            )
        elif encoder_type == "transformer":
            self.encoder_cell = TransformerEncoder(
                input_dim=input_dim,
                hidden_dim=hidden_dim,
                encoder_depth=rnn_depth,
                dropout=dropout,
                activation=activation,
                n_heads=n_heads,
                rpr = rpr
            )
        else:
            raise ValueError(f"Encoder type {encoder_type} not supported")
        # embedding layer
        if use_embeddings:
            if data_type == "notes":
                if not "sum" in embedding_dim.keys():
                    self.embeddings = nn.ModuleDict({
                        "pitch": nn.Embedding(NUMBER_OF_PITCHES, embedding_dim["pitch"]),
                        "duration": nn.Embedding(len(DURATIONS), embedding_dim["duration"]),
                        "metrical": nn.Embedding(METRICAL_LEVELS, embedding_dim["metrical"])
                    })
                else:
                    self.embeddings = nn.ModuleDict({
                        "pitch": nn.Embedding(NUMBER_OF_PITCHES, embedding_dim["sum"]),
                        "duration": nn.Embedding(len(DURATIONS), embedding_dim["sum"]),
                        "metrical": nn.Embedding(METRICAL_LEVELS, embedding_dim["sum"])
                    })

            elif data_type == "chords":
                # root_numbers, chord_forms, chord_extensions, duration_indices, metrical_indices
                if not "sum" in embedding_dim.keys():
                    self.embeddings = nn.ModuleDict({
                        "root": nn.Embedding(12, embedding_dim["root"]),
                        "form": nn.Embedding(len(CHORD_FORM), embedding_dim["form"]),
                        "ext": nn.Embedding(len(CHORD_EXTENSION), embedding_dim["ext"]),
                        "duration": nn.Embedding(len(JTB_DURATION), embedding_dim["duration"]),
                        "metrical": nn.Embedding(METRICAL_LEVELS, embedding_dim["metrical"])
                    })
                else:
                    self.embeddings = nn.ModuleDict({
                        "root": nn.Embedding(12, embedding_dim["sum"]),
                        "form": nn.Embedding(len(CHORD_FORM), embedding_dim["sum"]),
                        "ext": nn.Embedding(len(CHORD_EXTENSION), embedding_dim["sum"]),
                        "duration": nn.Embedding(len(JTB_DURATION), embedding_dim["sum"]),
                        "metrical": nn.Embedding(METRICAL_LEVELS, embedding_dim["sum"])
                    })
            else:
                raise ValueError(f"Data type {data_type} not supported")

    def forward(self, sequence, mask=None):
        if self.use_embeddings:
            # run embedding
            if self.data_type == "notes":
                # we are discarding rests information at [:,1] because it is in the pitch
                pitch = sequence[:,0]
                duration = sequence[:,2]
                metrical = sequence[:,3]
                pitch = self.embeddings["pitch"](pitch.long())
                duration = self.embeddings["duration"](duration.long())
                metrical = self.embeddings["metrical"](metrical.long())
                if not "sum" in self.embedding_dim.keys():
                    # concatenate embeddings
                    z = torch.hstack((pitch, duration, metrical))
                else:
                    # sum all embeddings
                    z = pitch + duration + metrical
            elif self.data_type == "chords":
                root = sequence[:,0]
                form = sequence[:,1]
                ext = sequence[:,2]
                duration = sequence[:,3]
                metrical = sequence[:,4]
                root = self.embeddings["root"](root.long())
                form = self.embeddings["form"](form.long())
                ext = self.embeddings["ext"](ext.long())
                duration = self.embeddings["duration"](duration.long())
                metrical = self.embeddings["metrical"](metrical.long())
                if not "sum" in self.embedding_dim.keys():
                    # concatenate embeddings
                    z = torch.hstack((root, form, ext, duration, metrical))
                else:
                    # sum all embeddings
                    z = root + form + ext + duration + metrical
        else:
            # one hot encoding
            z = get_feats_one_hot(sequence)

        if mask is None:
            z, _ = self.encoder_cell(z)
        else:
            z, _ = self.encoder_cell(z, src_mask= mask)

        # if self.dropout is not None:
        z = self.dropout(z)
        return z
      

class ArcDecoder(torch.nn.Module):
    def __init__(self, hidden_channels, activation=F.relu, dropout=0.3, biaffine=True, pretrain_mode = False):
        super().__init__()
        self.activation = activation
        self.biaffine = biaffine
        self.pretrain_mode = pretrain_mode
        self.root_linear = nn.Linear(1, hidden_channels) # linear to produce root features
        if not pretrain_mode: # normal functioning, predicting arcs
            if biaffine:
                self.lin1 = nn.Linear(hidden_channels, hidden_channels)
                self.lin2 = nn.Linear(hidden_channels, hidden_channels)
                self.bilinear = nn.Bilinear(hidden_channels , hidden_channels, 1)
            else:
                self.lin1 = nn.Linear(2*hidden_channels, hidden_channels)
                self.lin2 = nn.Linear(hidden_channels, 1)
        else: # pretraining mode, predicting chords
            self.lin_root = nn.Linear(hidden_channels, 12)
            self.lin_form = nn.Linear(hidden_channels, len(CHORD_FORM))
            self.lin_ext = nn.Linear(hidden_channels, len(CHORD_EXTENSION))
            self.lin_duration = nn.Linear(hidden_channels, len(JTB_DURATION))
            self.lin_metrical = nn.Linear(hidden_channels, METRICAL_LEVELS)
        self.dropout = nn.Dropout(dropout)
        self.norm = nn.LayerNorm(hidden_channels)
        

    def forward(self, z, pot_arcs):
        # add column for the root element
        # z = torch.cat((torch.ones((1,z.shape[1]),device=z.device),z), dim = 0)
        root_feat = self.root_linear(torch.ones((1,1), device=z.device))
        z = torch.vstack((root_feat,z))
        # proceed with the computation
        z = self.norm(z)
        if not self.pretrain_mode: # normal functioning, predicting arcs
            if self.biaffine:
                # get the embeddings of the starting and ending nodes, both of shape (num_pot_arcs, hidden_channels)
                input1 =  z[pot_arcs[:, 0]]
                input2 = z[pot_arcs[:, 1]]
                # pass through a linear layer, shape (num_pot_arcs, hidden_channels)
                input1 = self.lin1(input1)
                input2 = self.lin2(input2)
                # pass through an activation function, shape (num_pot_arcs, hidden_channels)
                input1 = self.activation(input1)
                input2 = self.activation(input2)
                # normalize
                input1 = self.norm(input1)
                input2 = self.norm(input2)
                # pass through a dropout layer, shape (num_pot_arcs, hidden_channels)
                input1 = self.dropout(input1)
                input2 = self.dropout(input2)
                # pass through the bilinear layer
                z = self.bilinear(input1, input2)
            else:
                # concat the embeddings of the two nodes, shape (num_pot_arcs, 2*hidden_channels)
                z = torch.cat([z[pot_arcs[:, 0]], z[pot_arcs[:, 1]]], dim=-1)
                # pass through a linear layer, shape (num_pot_arcs, hidden_channels)
                z = self.lin1(z)
                # pass through activation, shape (num_pot_arcs, hidden_channels)
                z = self.activation(z)
                # normalize
                z = self.norm(z)
                # dropout
                z = self.dropout(z)
                # pass through another linear layer, shape (num_pot_arcs, 1)
                z = self.lin2(z)
            # return a vector of shape (num_pot_arcs,)
            return z.view(-1)
        else: # pretraining mode, predicting chords
            out = {}
            out["root"] = self.lin_root(z)
            out["form"] = self.lin_form(z)
            out["ext"] = self.lin_ext(z)
            out["dur"] = self.lin_duration(z)
            out["met"] = self.lin_metrical(z)
            return out


class ArcPredictionModel(nn.Module):
    def __init__(self, input_dim, hidden_dim, num_layers, activation="relu", dropout=0.2, embedding_dim = {}, use_embedding = True, biaffine = False, encoder_type = "rnn", n_heads = 4, data_type = "notes", rpr = False, pretrain_mode = False):
        super().__init__()
        if activation == "relu":
            activation = F.relu
        elif activation == "gelu":
            activation = F.gelu
        else:
            raise ValueError("Unknown activation function")
        self.activation = activation
        # initialize the encoder
        self.encoder = NotesEncoder(input_dim, hidden_dim, num_layers, dropout, embedding_dim, use_embedding, encoder_type, activation=activation, n_heads=n_heads, data_type = data_type, rpr =rpr)
        # set the dimension that the decoder will expect
        if encoder_type == "rnn":
            self.decoder_dim = hidden_dim
        else: # transformer case, the hidden dim is the dimension of the input, i.e., the embeddings
            self.decoder_dim = input_dim
        self.pretrain_mode = pretrain_mode
        # initialize the decoder
        self.decoder = ArcDecoder(self.decoder_dim, activation=activation, dropout=dropout, biaffine=biaffine, pretrain_mode=pretrain_mode)

    def forward(self, note_features, pot_arcs, mask=None):
        z = self.encoder(note_features, mask)
        return self.decoder(z, pot_arcs)


def generate_square_subsequent_mask(sz: int) -> torch.Tensor:
    """Generates an upper-triangular matrix of -inf, with zeros on diag."""
    return torch.triu(torch.ones(sz, sz) * float('-inf'), diagonal=1)



