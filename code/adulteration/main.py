import os, sys, random, argparse, time, math, gzip
import cPickle as pickle
from collections import Counter

import numpy as np
import scipy
from sklearn.cross_validation import train_test_split
import theano
import theano.tensor as T

sys.path.append('../')
sys.path.append('../../../adulteration/wikipedia')
sys.path.append('../../../adulteration/model')
from nn import get_activation_by_name, create_optimization_updates, softmax
from nn import Layer, EmbeddingLayer, LSTM, RCNN, StrCNN, Dropout, apply_dropout
from utils import say, load_embedding_iterator

import hier_to_cat
import scoring
from wikipedia import *

np.set_printoptions(precision=3)
wiki_path = '../../../adulteration/wikipedia/'

def read_corpus_adulterants():
    with open(wiki_path+'input_to_outputs_adulterants.pkl', 'r') as f_in:
        input_to_outputs = pickle.load(f_in)
    corpus_x, corpus_y, hier_x = [], [], []
    input_keys = sorted(input_to_outputs.keys())
    adulterants = get_adulterants()
    input_tokens = input_to_tokens(input_keys, adulterants)
    ing_idx_to_hier_map = hier_to_cat.gen_ing_idx_to_hier_map(adulterants, adulterants=True)
    assert len(input_keys) == len(input_tokens)
    for i in range(len(input_keys)):
        inp = input_keys[i]
        tokens = input_tokens[i]
        hier = ing_idx_to_hier_map.get(i, np.zeros(3751))
        out = input_to_outputs[inp]
        if out.sum() <= 0:
            continue
        if len(tokens) > 5:
            corpus_x.append(tokens)
        else:
            corpus_x.append([])
        hier_x.append(hier.astype('float32'))
        normalized = out*1. / out.sum()
        assert np.isclose(normalized.sum(), 1, atol=1e-5)
        corpus_y.append(normalized.astype('float32'))
        #len_corpus_y.append(out.sum())
    assert len(corpus_x)==len(corpus_y)
    return np.array(corpus_x), np.array(corpus_y).astype('float32'), np.array(hier_x).astype('float32')

def read_corpus_ingredients(num_ingredients=5000):
    with open(wiki_path+'input_to_outputs.pkl', 'r') as f_in:
        input_to_outputs = pickle.load(f_in)
    corpus_x, corpus_y, hier_x = [], [], []
    #y_indptr = [0]
    #y_indices = []
    #y_data = []
    input_keys = sorted(input_to_outputs.keys())
    ings = get_ings(num_ingredients)
    input_tokens = input_to_tokens(input_keys, ings)
    ing_idx_to_hier_map = hier_to_cat.gen_ing_idx_to_hier_map(ings)
    assert len(input_keys) == len(input_tokens) == num_ingredients
    for i in range(num_ingredients):
        inp = input_keys[i]
        tokens = input_tokens[i]
        hier = ing_idx_to_hier_map.get(i, np.zeros(3751))
        assert len(hier) == 3751
        out = input_to_outputs[inp]
        if out.sum() <= 0:
            continue
        #y_data.extend(out)
        #y_indices.extend(range(len(out)))
        #y_indptr.append(len(out))
        if len(tokens) > 5:
            corpus_x.append(tokens)
        else:
            corpus_x.append([])
        hier_x.append(hier.astype('float32'))
        normalized = out*1. / out.sum()
        assert np.isclose(normalized.sum(), 1, atol=1e-5)
        corpus_y.append(normalized.astype('float32'))
        #len_corpus_y.append(out.sum())
    assert len(corpus_x)==len(corpus_y)==len(hier_x)
    #corpus_y = scipy.sparse.csr_matrix((y_data, y_indices, np.cumsum(y_indptr)))
    return np.array(corpus_x), np.array(corpus_y).astype('float32'), np.array(hier_x).astype('float32')

def read_corpus(path):
    with open(path) as fin:
        lines = fin.readlines()
    lines = [ x.strip().split() for x in lines ]
    lines = [ x for x in lines if x ]
    corpus_x = [ x[1:] for x in lines ]
    corpus_y = [ int(x[0]) for x in lines ]
    return corpus_x, corpus_y

def create_one_batch(ids, x, y, hier):
    batch_x = np.column_stack( [ x[i] for i in ids ] )
    batch_y = np.array( [ y[i] for i in ids ] )
    batch_hier = np.array( [ hier[i] for i in ids ] ) if hier is not None else None
    #batch_y = y[ids]
    assert batch_x.shape[1] == batch_y.shape[0] == batch_hier.shape[0]
    return batch_x, batch_y, batch_hier

# shuffle training examples and create mini-batches
def create_batches(perm, x, y, hier, batch_size):

    # sort sequences based on their length
    # permutation is necessary if we want different batches every epoch
    first_nonzero_idx = sum([1 for i in x if len(i)==0])
    lst = sorted(perm, key=lambda i: len(x[i]))[first_nonzero_idx:]
    batches_x = [ ]
    batches_y = [ ]
    batches_hier = [ ]
    size = batch_size
    ids = [ lst[0] ]
    for i in lst[1:]:
        if len(ids) < size and len(x[i]) == len(x[ids[0]]):
            ids.append(i)
        else:
            #print ids
            #print x, len(x)
            #print y, len(y)
            bx, by, bhier = create_one_batch(ids, x, y, hier)
            batches_x.append(bx)
            batches_y.append(by)
            batches_hier.append(bhier)
            ids = [ i ]
    bx, by, bhier = create_one_batch(ids, x, y, hier)
    batches_x.append(bx)
    batches_y.append(by)
    batches_hier.append(bhier)

    # shuffle batches
    batch_perm = range(len(batches_x))
    random.shuffle(batch_perm)
    batches_x = [ batches_x[i] for i in batch_perm ]
    batches_y = [ batches_y[i] for i in batch_perm ]
    batches_hier = [ batches_hier[i] for i in batch_perm ]
    assert len(batches_x) == len(batches_y) == len(batches_hier)
    return batches_x, batches_y, batches_hier

def save_predictions(predict_model, train, dev, test):
    trainx, trainy = train
    devx, devy = dev
    testx, testy = test
    for x_data, data_name in [(trainx, 'train'), (devx, 'dev'), (testx, 'test')]:
        results = []
        for x_idx, x_for_predict in enumerate(x_data):
            if len(x_for_predict) > 0:
                p_y_given_x = predict_model(np.vstack(x_for_predict))[0]
                results.append(p_y_given_x)
            else:
                results.append(np.zeros(len(results[0])))
        np.save('{}_pred.npy'.format(data_name), np.array(results))


def evaluate(x_data, y_data, predict_model):
    """Compute the MAP of the data."""
    ing_cat_pair_map = {}
    for x_idx, x in enumerate(x_data):
        for y_idx, out in enumerate(y_data[x_idx]):
            if out > 0:
                ing_cat_pair_map[(x_idx, y_idx)] = True

    valid_ing_indices, results = [], []
    for x_idx, x_for_predict in enumerate(x_data):
        if len(x_for_predict) > 0:
            p_y_given_x = predict_model(np.vstack(x_for_predict))[0]
            valid_ing_indices.append(x_idx)
            results.append(p_y_given_x)
    valid_ing_indices = np.array(valid_ing_indices)
    avg_true_results = scoring.gen_avg_true_results(valid_ing_indices)

    results = np.array(results)
    print "Random:"
    scoring.evaluate_map(valid_ing_indices, results, ing_cat_pair_map, random=True)
    print "Avg True Results:"
    scoring.evaluate_map(valid_ing_indices, avg_true_results, ing_cat_pair_map, random=False)
    print "Model:"
    scoring.evaluate_map(valid_ing_indices, results, ing_cat_pair_map, random=False)

class Model:
    def __init__(self, args, embedding_layer, nclasses):
        self.args = args
        self.embedding_layer = embedding_layer
        self.nclasses = nclasses

    def ready(self):
        args = self.args
        embedding_layer = self.embedding_layer
        self.n_hidden = args.hidden_dim
        self.n_in = embedding_layer.n_d
        dropout = self.dropout = theano.shared(
                np.float64(args.dropout_rate).astype(theano.config.floatX)
            )

        # x is length * batch_size
        # y is batch_size * num_cats
        # hier is batch_size * hier_dim
        self.x = T.imatrix('x')
        self.hier = T.fmatrix('hier')
        #self.y = T.ivector('y')
        self.y = T.fmatrix('y')
        self.y_len = T.ivector()

        x = self.x
        y = self.y
        hier = self.hier
        y_len = self.y_len
        n_hidden = self.n_hidden
        n_in = self.n_in

        # fetch word embeddings
        # (len * batch_size) * n_in
        slices  = embedding_layer.forward(x.ravel())
        self.slices = slices

        # 3-d tensor, len * batch_size * n_in
        slices = slices.reshape( (x.shape[0], x.shape[1], n_in) )

        # stacking the feature extraction layers
        pooling = args.pooling
        depth = args.depth
        layers = self.layers = [ ]
        prev_output = slices
        prev_output = apply_dropout(prev_output, dropout, v2=True)
        size = 0
        softmax_inputs = [ ]
        activation = get_activation_by_name(args.act)
        for i in range(depth):
            if args.layer.lower() == "lstm":
                print "Layer: LSTM"
                layer = LSTM(
                            n_in = n_hidden if i > 0 else n_in,
                            n_out = n_hidden
                        )
            elif args.layer.lower() == "strcnn":
                print "Layer: StrCNN"
                layer = StrCNN(
                            n_in = n_hidden if i > 0 else n_in,
                            n_out = n_hidden,
                            activation = activation,
                            decay = args.decay,
                            order = args.order
                        )
            elif args.layer.lower() == "rcnn":
                print "Layer: RCNN"
                layer = RCNN(
                            n_in = n_hidden if i > 0 else n_in,
                            n_out = n_hidden,
                            activation = activation,
                            order = args.order,
                            mode = args.mode
                        )
            else:
                raise Exception("unknown layer type: {}".format(args.layer))

            layers.append(layer)
            prev_output = layer.forward_all(prev_output)
            if pooling:
                softmax_inputs.append(T.sum(prev_output, axis=0)) # summing over columns
            else:
                softmax_inputs.append(prev_output[-1])
            prev_output = apply_dropout(prev_output, dropout)
            size += n_hidden

        # final feature representation is the concatenation of all extraction layers
        if pooling:
            softmax_input = T.concatenate(softmax_inputs, axis=1) / x.shape[0]
        else:
            softmax_input = T.concatenate(softmax_inputs, axis=1)
        
        # HIER CODE
        #hier = T.sum(hier, axis=0) / x.shape[0]
        #softmax_input = T.concatenate([softmax_input, hier], axis=0)
        ###

        softmax_input = apply_dropout(softmax_input, dropout, v2=True)

        # feed the feature repr. to the softmax output layer
        layers.append( Layer(
                n_in = size,
                n_out = self.nclasses,
                activation = softmax,
                has_bias = False
        ) )

        for l,i in zip(layers, range(len(layers))):
            say("layer {}: n_in={}\tn_out={}\n".format(
                i, l.n_in, l.n_out
            ))

        # unnormalized score of y given x
        self.p_y_given_x = layers[-1].forward(softmax_input)
        self.pred = T.argmax(self.p_y_given_x, axis=1)

        self.nll_loss = T.mean( T.nnet.categorical_crossentropy(
                                    self.p_y_given_x,
                                    y
                            ))

        # adding regularizations
        self.l2_sqr = None
        self.params = [ ]
        for layer in layers:
            self.params += layer.params
        for p in self.params:
            if self.l2_sqr is None:
                self.l2_sqr = args.l2_reg * T.sum(p**2)
            else:
                self.l2_sqr += args.l2_reg * T.sum(p**2)

        nparams = sum(len(x.get_value(borrow=True).ravel()) \
                        for x in self.params)
        say("total # parameters: {}\n".format(nparams))

    def save_model(self, path, args):
         # append file suffix
        if not path.endswith(".pkl.gz"):
            if path.endswith(".pkl"):
                path += ".gz"
            else:
                path += ".pkl.gz"

        with gzip.open(path, "wb") as fout:
            pickle.dump(
                ([ x.get_value() for x in self.params ], args),
                fout,
                protocol = pickle.HIGHEST_PROTOCOL
            )

    def load_model(self, path):
        if not os.path.exists(path):
            if path.endswith(".pkl"):
                path += ".gz"
            else:
                path += ".pkl.gz"

        with gzip.open(path, "rb") as fin:
            param_values, args = pickle.load(fin)

        assert self.args.layer.lower() == args.layer.lower()
        assert self.args.depth == args.depth
        assert self.args.hidden_dim == args.hidden_dim
        for x,v in zip(self.params, param_values):
            x.set_value(v)

    def eval_accuracy(self, preds, golds):
        fine = sum([ sum(p == y) for p,y in zip(preds, golds) ]) + 0.0
        fine_tot = sum( [ len(y) for y in golds ] )
        return fine/fine_tot


    def train(self, train, dev, test, hier):
        print "I AM HERE"
        args = self.args
        trainx, trainy = train
        if hier is None:
            train_hier_x, dev_hier_x, test_hier_x = None, None, None
        else:
            train_hier_x, dev_hier_x, test_hier_x = hier
        batch_size = args.batch

        if dev:
            dev_batches_x, dev_batches_y, dev_batches_hier = create_batches(
                    range(len(dev[0])),
                    dev[0],
                    dev[1],
                    dev_hier_x,
                    batch_size
            )

        if test:
            test_batches_x, test_batches_y, test_batches_hier = create_batches(
                    range(len(test[0])),
                    test[0],
                    test[1],
                    test_hier_x,
                    batch_size
            )

        cost = self.nll_loss + self.l2_sqr

        updates, lr, gnorm = create_optimization_updates(
                cost = cost,
                params = self.params,
                lr = args.learning_rate,
                method = args.learning
            )[:3]
        print "I AM HERE"
        train_model = theano.function(
             inputs = [self.x, self.y],#, self.hier],
             outputs = [ cost, gnorm ],
             updates = updates,
             allow_input_downcast = True
        )
        print "I AM HERE"
        predict_model = theano.function(
             inputs = [self.x, self.hier],
             outputs = self.p_y_given_x,
             allow_input_downcast = True
        )
        print "I AM HERE"

        eval_acc = theano.function(
             inputs = [self.x, self.hier],
             outputs = self.pred,
             allow_input_downcast = True
        )
        print "I AM HERE"
        unchanged = 0
        best_dev = 0.0
        dropout_prob = np.float64(args.dropout_rate).astype(theano.config.floatX)

        start_time = time.time()
        eval_period = args.eval_period

        perm = range(len(trainx))

        say(str([ "%.2f" % np.linalg.norm(x.get_value(borrow=True)) for x in self.params ])+"\n")
        for epoch in xrange(args.max_epochs):
            print "I AM HERE"
            unchanged += 1
            #if dev and unchanged > 30: return
            train_loss = 0.0

            random.shuffle(perm)
            batches_x, batches_y, batches_hier = create_batches(perm, trainx, trainy, train_hier_x, batch_size)
            print "I AM HERE"
            N = len(batches_x)
            for i in xrange(N):

                if i % 100 == 0:
                    sys.stdout.write("\r%d" % i)
                    sys.stdout.flush()

                x = batches_x[i]
                y = batches_y[i]
                hier = batches_hier[i]
                y_len = np.array([j.sum() for j in batches_y[i]])
                #y = y.toarray()

                assert x.dtype in ['float32', 'int32']
                assert y.dtype in ['float32', 'int32']
                assert hier.dtype in ['float32', 'int32']
                print x[0]
                print y[0]
                print hier[0]
                va, grad_norm = train_model(x, y, hier)
                train_loss += va

                # debug
                if math.isnan(va):
                    print ""
                    print i-1, i
                    print x
                    print y
                    return

                if (i == N-1) or (eval_period > 0 and (i+1) % eval_period == 0):
                    self.dropout.set_value(0.0)

                    say( "\n" )
                    say( "Epoch %.1f\tloss=%.4f\t|g|=%s  [%.2fm]\n" % (
                            epoch + (i+1)/(N+0.0),
                            train_loss / (i+1),
                            float(grad_norm),
                            (time.time()-start_time) / 60.0
                    ))
                    say(str([ "%.2f" % np.linalg.norm(x.get_value(borrow=True)) for x in self.params ])+"\n")

                    """
                    if dev:
                        preds = [ eval_acc(x) for x in dev_batches_x ]
                        nowf_dev = self.eval_accuracy(preds, dev_batches_y)
                        if nowf_dev > best_dev:
                            unchanged = 0
                            best_dev = nowf_dev
                            if args.model:
                                self.save_model(args.model, args)

                        say("\tdev accuracy=%.4f\tbest=%.4f\n" % (
                                nowf_dev,
                                best_dev
                        ))
                        if args.test and nowf_dev == best_dev:
                            preds = [ eval_acc(x) for x in test_batches_x ]
                            nowf_test = self.eval_accuracy(preds, test_batches_y)
                            say("\ttest accuracy=%.4f\n" % (
                                    nowf_test,
                            ))

                        if best_dev > nowf_dev + 0.05:
                            return
                    """

                    self.dropout.set_value(dropout_prob)

                    start_time = time.time()

            #print "Length of trainx: ", len(trainx)
            #for x_idx, x_for_predict in enumerate(trainx[3233:3236]):
            #    if len(x_for_predict) > 0:
            #        p_y_given_x = predict_model(np.vstack(x_for_predict))
            #        print x_idx, p_y_given_x
            print "======= Training evaluation ========"
            evaluate(trainx, trainy, predict_model)
            print "======= Validation evaluation ========"
            evaluate(dev[0], dev[1], predict_model)
            print "======= Adulteration evaluation ========"
            evaluate(test[0], test[1], predict_model)

        save_predictions(predict_model, train, dev, test)


def main(args):
    print args

    model = None

    assert args.embedding, "Pre-trained word embeddings required."

    if '.pkl' in args.embedding:
        with open(args.embedding, 'rb') as f:
            embedding = pickle.load(f)
            if '<unk>' not in embedding:
                embedding['<unk>'] = np.zeros(len(embedding['</s>']))
    else:
        embedding = load_embedding_iterator(args.embedding)

    embedding_layer = EmbeddingLayer(
                n_d = args.hidden_dim,
                vocab = [ "<unk>" ],
                embs = embedding       
            )

    train_hier_x = dev_hier_x = test_hier_x = None
    if args.train:
        train_x_text, train_y, train_hier_x = read_corpus_ingredients()
        num_data = len(train_x_text)
        print "Num data points:", num_data
        if args.dev:
            train_indices, dev_indices = train_test_split(
                range(num_data), test_size=1/3., random_state=42)
            dev_x_text = train_x_text[dev_indices]
            train_x_text = train_x_text[train_indices]
            dev_y = train_y[dev_indices]
            train_y = train_y[train_indices]
            dev_hier_x = train_hier_x[dev_indices]
            train_hier_x = train_hier_x[train_indices]
        train_x = [ embedding_layer.map_to_ids(x) for x in train_x_text ]

    if args.dev:
        #dev_x, dev_y = read_corpus(args.dev)
        dev_x = [ embedding_layer.map_to_ids(x) for x in dev_x_text ]

    if args.test:
        test_x_text, test_y, test_hier_x = read_corpus_adulterants()
        test_x = [ embedding_layer.map_to_ids(x) for x in test_x_text ]

    if args.train:
        model = Model(
                    args = args,
                    embedding_layer = embedding_layer,
                    nclasses = len(train_y[0]) #max(train_y.data)+1
            )
        model.ready()
        print train_x[0].dtype, train_hier_x[0].dtype, dev_hier_x[0].dtype, test_hier_x[0].dtype
        model.train(
                (train_x, train_y),
                (dev_x, dev_y) if args.dev else None,
                (test_x, test_y) if args.test else None,
                (train_hier_x, dev_hier_x, test_hier_x) if args.use_hier else None
            )


if __name__ == "__main__":
    argparser = argparse.ArgumentParser(sys.argv[0])
    argparser.add_argument("--train",
            type = str,
            default = "",
            help = "path to training data"
        )
    argparser.add_argument("--dev",
            type = str,
            default = "",
            help = "path to development data"
        )
    argparser.add_argument("--test",
            type = str,
            default = "",
            help = "path to test data"
        )
    argparser.add_argument("--hidden_dim", "-d",
            type = int,
            default = 200,
            help = "hidden dimensions"
        )
    argparser.add_argument("--decay",
            type = float,
            default = 0.3
        )
    argparser.add_argument("--learning",
            type = str,
            default = "adagrad",
            help = "learning method (sgd, adagrad, ...)"
        )
    argparser.add_argument("--learning_rate",
            type = float,
            default = "0.01",
            help = "learning rate"
        )
    argparser.add_argument("--max_epochs",
            type = int,
            default = 100,
            help = "maximum # of epochs"
        )
    argparser.add_argument("--eval_period",
            type = int,
            default = -1,
            help = "evaluate on dev every period"
        )
    argparser.add_argument("--dropout_rate",
            type = float,
            default = 0.0,
            help = "dropout probability"
        )
    argparser.add_argument("--l2_reg",
            type = float,
            default = 0.00001
        )
    argparser.add_argument("--embedding",
            type = str,
            default = ""
        )
    argparser.add_argument("--batch",
            type = int,
            default = 15,
            help = "mini-batch size"
        )
    argparser.add_argument("--depth",
            type = int,
            default = 3,
            help = "number of feature extraction layers (min:1)"
        )
    argparser.add_argument("--order",
            type = int,
            default = 3,
            help = "when the order is k, we use up tp k-grams (k=1,2,3)"
        )
    argparser.add_argument("--act",
            type = str,
            default = "relu",
            help = "activation function (none, relu, tanh)"
        )
    argparser.add_argument("--layer",
            type = str,
            default = "rcnn"
        )
    argparser.add_argument("--mode",
            type = int,
            default = 1
        )
    argparser.add_argument("--seed",
            type = int,
            default = -1,
            help = "random seed of the model"
        )
    argparser.add_argument("--model",
            type = str,
            default = "",
            help = "save model to this file"
        )
    argparser.add_argument("--pooling",
            type = int,
            default = 1,
            help = "whether to use mean pooling or take the last vector"
        )
    argparser.add_argument("--use_hier",
            action='store_true',
            help = "use hierarchy"
        )
    args = argparser.parse_args()
    main(args)

