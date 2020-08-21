import os
import time
import argparse
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
import torch.optim as optim
from torch.optim.lr_scheduler import ReduceLROnPlateau
import torch.backends.cudnn as cudnn
import dgl

from graph4nlp.pytorch.datasets.trec import TrecDataset
from graph4nlp.pytorch.modules.graph_construction.dependency_graph_construction import DependencyBasedGraphConstruction
from graph4nlp.pytorch.modules.graph_construction.constituency_graph_construction import ConstituencyBasedGraphConstruction
from graph4nlp.pytorch.modules.graph_construction import NodeEmbeddingBasedGraphConstruction, NodeEmbeddingBasedRefinedGraphConstruction
from graph4nlp.pytorch.modules.prediction.generation.StdRNNDecoder import StdRNNDecoder
from graph4nlp.pytorch.modules.graph_embedding.gat import GAT
from graph4nlp.pytorch.modules.prediction.classification.graph_classification import FeedForwardNN
from graph4nlp.pytorch.modules.evaluation.base import EvaluationMetricBase
from graph4nlp.pytorch.modules.evaluation.accuracy import Accuracy
from graph4nlp.pytorch.modules.utils.generic_utils import to_cuda, EarlyStopping
from graph4nlp.pytorch.modules.loss.general_loss import GeneralLoss
import torch.multiprocessing
torch.multiprocessing.set_sharing_strategy('file_system')


class TextClassifier(nn.Module):
    def __init__(self, vocab, config):
        super(TextClassifier, self).__init__()
        self.config = config
        self.vocab = vocab
        embedding_style = {'word_emb_type': 'w2v', 'node_edge_emb_strategy': "mean",
                           'seq_info_encode_strategy': "bilstm"}

        if config.graph_type == 'dependency':
            self.graph_topology = DependencyBasedGraphConstruction(embedding_style=embedding_style,
                                                                   vocab=vocab.in_word_vocab,
                                                                   hidden_size=config.num_hidden,
                                                                   dropout=0.2,
                                                                   fix_word_emb=config.fix_word_emb,
                                                                   device=config.device)
        elif config.graph_type == 'constituency':
            self.graph_topology = ConstituencyBasedGraphConstruction(embedding_style=embedding_style,
                                                                   vocab=vocab.in_word_vocab,
                                                                   hidden_size=config.num_hidden,
                                                                   dropout=0.2,
                                                                   fix_word_emb=config.fix_word_emb,
                                                                   device=config.device)
        elif config.graph_type == 'node_emb':
            self.graph_topology = NodeEmbeddingBasedGraphConstruction(
                                    vocab.in_word_vocab,
                                    embedding_style,
                                    sim_metric_type=config.gl_metric_type,
                                    num_heads=config.gl_num_heads,
                                    top_k_neigh=config.gl_top_k,
                                    epsilon_neigh=config.gl_epsilon,
                                    smoothness_ratio=config.gl_smoothness_ratio,
                                    connectivity_ratio=config.gl_connectivity_ratio,
                                    sparsity_ratio=config.gl_sparsity_ratio,
                                    input_size=config.num_hidden,
                                    hidden_size=config.gl_num_hidden,
                                    fix_word_emb=config.fix_word_emb,
                                    dropout=None,
                                    device=config.device)
        elif config.graph_type == 'node_emb_refined':
            self.graph_topology = NodeEmbeddingBasedRefinedGraphConstruction(
                                    vocab.in_word_vocab,
                                    embedding_style,
                                    config.init_adj_alpha,
                                    sim_metric_type=config.gl_metric_type,
                                    num_heads=config.gl_num_heads,
                                    top_k_neigh=config.gl_top_k,
                                    epsilon_neigh=config.gl_epsilon,
                                    smoothness_ratio=config.gl_smoothness_ratio,
                                    connectivity_ratio=config.gl_connectivity_ratio,
                                    sparsity_ratio=config.gl_sparsity_ratio,
                                    input_size=config.num_hidden,
                                    hidden_size=config.gl_num_hidden,
                                    fix_word_emb=config.fix_word_emb,
                                    dropout=None,
                                    device=config.device)
        else:
            raise RuntimeError('Unknown graph_type: {}'.format(config.graph_type))

        self.word_emb = self.graph_topology.embedding_layer.word_emb_layers[0].word_emb_layer


        heads = [config.num_heads] * (config.num_layers - 1) + [config.num_out_heads]
        self.gnn = GAT(config.num_layers,
                    config.num_hidden,
                    config.num_hidden,
                    config.num_hidden,
                    heads,
                    direction_option=config.direction_option,
                    feat_drop=config.in_drop,
                    attn_drop=config.attn_drop,
                    negative_slope=config.negative_slope,
                    residual=config.residual,
                    activation=F.elu)
        self.clf = FeedForwardNN(2 * config.num_hidden if config.direction_option == 'bi_sep' else config.num_hidden,
                        config.num_classes,
                        [config.num_hidden],
                        graph_pool_type=config.graph_pooling,
                        dim=config.num_hidden,
                        use_linear_proj=True)

        self.loss = GeneralLoss('CrossEntropy')


    def forward(self, graph_list, tgt=None, require_loss=True):
        # graph embedding construction
        if self.config.graph_type == 'node_emb':
            batch_gd = self.graph_topology(graph_list)
        elif self.config.graph_type == 'node_emb_refined':
            batch_gd = self.graph_topology(graph_list, graph_list, node_mask=None)
        else:
            batch_gd = self.graph_topology(graph_list)

        # run GNN
        self.gnn(batch_gd)

        # run graph classifier
        self.clf(batch_gd)
        logits = batch_gd.graph_attributes['logits']

        if require_loss:
            loss = self.loss(logits, tgt)
            return logits, loss
        else:
            return logits


class ModelHandler:
    def __init__(self, config):
        super(ModelHandler, self).__init__()
        self.config = config
        self._build_dataloader()
        self._build_model()
        self._build_optimizer()
        self._build_evaluation()

    def _build_dataloader(self):
        if self.config.graph_type == 'dependency':
            topology_builder = DependencyBasedGraphConstruction
            graph_type = 'static'
        elif self.config.graph_type == 'constituency':
            topology_builder = ConstituencyBasedGraphConstruction
            graph_type = 'static'
        elif self.config.graph_type == 'node_emb':
            topology_builder = NodeEmbeddingBasedGraphConstruction
            graph_type = 'dynamic'
        elif self.config.graph_type == 'node_emb_refined':
            topology_builder = NodeEmbeddingBasedRefinedGraphConstruction
            graph_type = 'dynamic'
        else:
            raise RuntimeError('Unknown graph_type: {}'.format(config.graph_type))

        topology_subdir = '{}_based_graph'.format(self.config.graph_type)
        dataset = TrecDataset(root_dir="examples/pytorch/text_classification/data/trec",
                              topology_builder=topology_builder,
                              topology_subdir=topology_subdir,
                              graph_type=graph_type,
                              pretrained_word_emb_file=self.config.pre_word_emb_file,
                              val_split_ratio=self.config.val_split_ratio)
        self.train_dataloader = DataLoader(dataset.train, batch_size=self.config.batch_size, shuffle=True,
                                           num_workers=12,
                                           collate_fn=dataset.collate_fn)
        self.val_dataloader = DataLoader(dataset.val, batch_size=self.config.batch_size, shuffle=False,
                                          num_workers=12,
                                          collate_fn=dataset.collate_fn)
        self.test_dataloader = DataLoader(dataset.test, batch_size=self.config.batch_size, shuffle=False,
                                          num_workers=12,
                                          collate_fn=dataset.collate_fn)
        self.vocab = dataset.vocab_model
        self.config.num_classes = dataset.num_classes
        self.num_train = len(dataset.train)
        self.num_val = len(dataset.val)
        self.num_test = len(dataset.test)
        print('Train size: {}, Val size: {}, Test size: {}'
            .format(self.num_train, self.num_val, self.num_test))

    def _build_model(self):
        self.model = TextClassifier(self.vocab, self.config).to(self.config.device)

    def _build_optimizer(self):
        parameters = [p for p in self.model.parameters() if p.requires_grad]
        self.optimizer = optim.Adam(parameters, lr=self.config.lr)
        self.stopper = EarlyStopping('{}.{}'.format(self.config.save_model_path, self.config.seed), patience=self.config.patience)
        self.scheduler = ReduceLROnPlateau(self.optimizer, mode='max', factor=self.config.lr_reduce_factor, \
            patience=self.config.lr_patience, verbose=True)

    def _build_evaluation(self):
        self.metric = Accuracy(['accuracy'])

    def train(self):
        dur = []
        for epoch in range(self.config.epochs):
            self.model.train()
            train_loss = []
            train_acc = []
            t0 = time.time()
            for i, data in enumerate(self.train_dataloader):
                graph_list, tgt = data
                tgt = to_cuda(tgt, self.config.device)
                logits, loss = self.model(graph_list, tgt, require_loss=True)
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()
                train_loss.append(loss.item())

                pred = torch.max(logits, dim=-1)[1].cpu()
                train_acc.append(self.metric.calculate_scores(ground_truth=tgt.cpu(), predict=pred.cpu())[0])
                dur.append(time.time() - t0)

            val_acc = self.evaluate(self.val_dataloader)
            self.scheduler.step(val_acc)
            print("Epoch: [{} / {}] | Time: {:.4f}s | Loss: {:.4f} | Train Acc: {:.4f} | Val Acc: {:.4f}".
              format(epoch + 1, self.config.epochs, np.mean(dur), np.mean(train_loss), np.mean(train_acc), val_acc))

            if self.stopper.step(val_acc, self.model):
                break

    def evaluate(self, dataloader):
        self.model.eval()
        with torch.no_grad():
            pred_collect = []
            gt_collect = []
            for i, data in enumerate(dataloader):
                graph_list, tgt = data
                logits = self.model(graph_list, require_loss=False)
                pred_collect.append(logits)
                gt_collect.append(tgt)

            pred_collect = torch.max(torch.cat(pred_collect, 0), dim=-1)[1].cpu()
            gt_collect = torch.cat(gt_collect, 0).cpu()
            score = self.metric.calculate_scores(ground_truth=gt_collect, predict=pred_collect)[0]

            return score

    def test(self):
        t0 = time.time()
        acc = self.evaluate(self.test_dataloader)
        dur = time.time() - t0
        print("Test examples: {} | Time: {:.4f}s |  Test Acc: {:.4f}".
          format(self.num_test, dur, acc))

        return acc

def main(args):
    # configure
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    if not args.no_cuda and torch.cuda.is_available():
        print('[ Using CUDA ]')
        args.device = torch.device('cuda' if args.gpu < 0 else 'cuda:%d' % args.gpu)
        cudnn.benchmark = True
        torch.cuda.manual_seed(args.seed)
    else:
        args.device = torch.device('cpu')

    runner = ModelHandler(args)
    runner.train()
    # restored best saved model
    runner.stopper.load_checkpoint(runner.model)
    runner.test()

    print('Removed best saved model file to save disk space')
    os.remove(runner.stopper.save_model_path)


if __name__ == "__main__":
    # Settings
    parser = argparse.ArgumentParser()
    parser.add_argument("--no_cuda", action="store_true", default=False,
                        help="use CPU")
    parser.add_argument("--gpu", type=int, default=-1,
                        help="which GPU to use.")
    # graph construction
    parser.add_argument("--graph_type", type=str, default='dependency',
                        help="graph construction type (`dependency`, `constituency`, `node_emb`, `node_emb_refined`)")
    # dynamic graph construction
    parser.add_argument("--gl_metric_type", type=str, default='weighted_cosine',
                        help=r"similarity metric type for dynamic graph construction")
    parser.add_argument("--gl_top_k", type=int,
                        help="top k for graph sparsification")
    parser.add_argument("--gl_num_hidden", type=int, default=128,
                        help="number of hidden units for dynamic graph construction")
    parser.add_argument("--gl_num_heads", type=int, default=1,
                        help="num of heads for dynamic graph construction")
    parser.add_argument("--gl_epsilon", type=float,
                        help="epsilon for graph sparsification")
    parser.add_argument("--gl_smoothness_ratio", type=float,
                        help="smoothness ratio for graph regularization loss")
    parser.add_argument("--gl_connectivity_ratio", type=float,
                        help="connectivity ratio for graph regularization loss")
    parser.add_argument("--gl_sparsity_ratio", type=float,
                        help="sparsity ratio for graph regularization loss")
    parser.add_argument("--init_adj_alpha", type=float, default=0.8,
                        help="alpha ratio for combining initial graph adjacency matrix")
    # gnn
    parser.add_argument("--graph_pooling", type=str, default='max_pool',
                        help="graph pooling (`avg_pool`, `max_pool`)")
    parser.add_argument("--direction_option", type=str, default='uni',
                        help="direction type (`uni`, `bi_fuse`, `bi_sep`)")
    parser.add_argument("--num_heads", type=int, default=8,
                        help="number of hidden attention heads")
    parser.add_argument("--num_out_heads", type=int, default=1,
                        help="number of output attention heads")
    parser.add_argument("--num_layers", type=int, default=2,
                        help="number of hidden layers")
    parser.add_argument("--num_hidden", type=int, default=128,
                        help="number of hidden units")
    parser.add_argument("--residual", action="store_true", default=False,
                        help="use residual connection")
    parser.add_argument("--in_drop", type=float, default=.6,
                        help="input feature dropout")
    parser.add_argument("--attn_drop", type=float, default=.6,
                        help="attention dropout")
    # training
    parser.add_argument("--lr", type=float, default=0.001,
                        help="learning rate")
    parser.add_argument('--weight_decay', type=float, default=5e-4,
                        help="weight decay")
    parser.add_argument('--negative_slope', type=float, default=0.2,
                        help="the negative slope of leaky relu")
    parser.add_argument("--patience", type=int, default=10,
                        help="early stopping patience")
    parser.add_argument("--lr_patience", type=int, default=2,
                        help="learning rate patience")
    parser.add_argument("--lr_reduce_factor", type=float, default=0.5,
                        help="learning rate reduce factor")
    parser.add_argument('--drop_ratio', type=float, default=0.5,
                        help='dropout ratio (default: 0.5)')
    parser.add_argument('--batch_size', type=int, default=32,
                        help='input batch size for training (default: 32)')
    parser.add_argument('--val_split_ratio', type=float, default=0.2,
                        help='validation set split ratio (default: 0.2)')
    parser.add_argument('--epochs', type=int, default=100,
                        help='number of epochs to train (default: 100)')
    parser.add_argument('--seed', type=int, default=1234,
                        help='random seed (default: 1234)')
    parser.add_argument('--num_workers', type=int, default=0,
                        help='number of workers (default: 0)')
    # parser.add_argument('--dataset', type=str, default="",
    #                     help='dataset name')
    parser.add_argument("--fix_word_emb", action="store_true", default=False,
                        help="fix pretrained word embeddings")
    parser.add_argument('--pre_word_emb_file', type=str,
                        help='path to the pretrained word embedding file')
    parser.add_argument('--save_model_path', type=str, default="checkpoint",
                        help="path to the best saved model")

    args = parser.parse_args()

    main(args)

