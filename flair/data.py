from abc import abstractmethod
from typing import List, Dict, Union, Iterable

import torch
import logging

from collections import Counter
from collections import defaultdict

from segtok.segmenter import split_single
from segtok.tokenizer import split_contractions
from segtok.tokenizer import word_tokenizer

import itertools
from inspect import isgeneratorfunction

log = logging.getLogger('flair')


class Dictionary:
    """
    This class holds a dictionary that maps strings to IDs, used to generate one-hot encodings of strings.
    """

    def __init__(self, add_unk=True):
        # init dictionaries
        self.item2idx: Dict[str, int] = {}
        self.idx2item: List[str] = []

        # in order to deal with unknown tokens, add <unk>
        if add_unk:
            self.add_item('<unk>')

    def add_item(self, item: str) -> int:
        """
        add string - if already in dictionary returns its ID. if not in dictionary, it will get a new ID.
        :param item: a string for which to assign an id.
        :return: ID of string
        """
        item = item.encode('utf-8')
        if item not in self.item2idx:
            self.idx2item.append(item)
            self.item2idx[item] = len(self.idx2item) - 1
        return self.item2idx[item]

    def get_idx_for_item(self, item: str) -> int:
        """
        returns the ID of the string, otherwise 0
        :param item: string for which ID is requested
        :return: ID of string, otherwise 0
        """
        item = item.encode('utf-8')
        if item in self.item2idx.keys():
            return self.item2idx[item]
        else:
            return 0

    def get_items(self) -> List[str]:
        items = []
        for item in self.idx2item:
            items.append(item.decode('UTF-8'))
        return items

    def __len__(self) -> int:
        return len(self.idx2item)

    def get_item_for_index(self, idx):
        return self.idx2item[idx].decode('UTF-8')

    def save(self, savefile):
        import pickle
        with open(savefile, 'wb') as f:
            mappings = {
                'idx2item': self.idx2item,
                'item2idx': self.item2idx
            }
            pickle.dump(mappings, f)

    @classmethod
    def load_from_file(cls, filename: str):
        import pickle
        dictionary: Dictionary = Dictionary()
        with open(filename, 'rb') as f:
            mappings = pickle.load(f, encoding='latin1')
            idx2item = mappings['idx2item']
            item2idx = mappings['item2idx']
            dictionary.item2idx = item2idx
            dictionary.idx2item = idx2item
        return dictionary

    @classmethod
    def load(cls, name: str):
        from flair.file_utils import cached_path
        if name == 'chars' or name == 'common-chars':
            base_path = 'https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/models/common_characters'
            char_dict = cached_path(base_path, cache_dir='datasets')
            return Dictionary.load_from_file(char_dict)

        return Dictionary.load_from_file(name)


class Label:
    """
    This class represents a label of a sentence. Each label has a value and optionally a confidence score. The
    score needs to be between 0.0 and 1.0. Default value for the score is 1.0.
    """

    def __init__(self, value: str, score: float = 1.0):
        self.value = value
        self.score = score
        super().__init__()

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        if not value and value != '':
            raise ValueError('Incorrect label value provided. Label value needs to be set.')
        else:
            self._value = value

    @property
    def score(self):
        return self._score

    @score.setter
    def score(self, score):
        if 0.0 <= score <= 1.0:
            self._score = score
        else:
            self._score = 1.0

    def to_dict(self):
        return {
            'value': self.value,
            'confidence': self.score
        }

    def __str__(self):
        return "{} ({})".format(self._value, self._score)

    def __repr__(self):
        return "{} ({})".format(self._value, self._score)


class Token:
    """
    This class represents one word in a tokenized sentence. Each token may have any number of tags. It may also point
    to its head in a dependency tree.
    """

    def __init__(self,
                 text: str,
                 idx: int = None,
                 head_id: int = None,
                 whitespace_after: bool = True,
                 start_position: int = None
                 ):
        self.text: str = text
        self.idx: int = idx
        self.head_id: int = head_id
        self.whitespace_after: bool = whitespace_after

        self.start_pos = start_position
        self.end_pos = start_position + len(text) if start_position is not None else None

        self.sentence: Sentence = None
        self._embeddings: Dict = {}
        self.tags: Dict[str, Label] = {}

    def add_tag_label(self, tag_type: str, tag: Label):
        self.tags[tag_type] = tag

    def add_tag(self, tag_type: str, tag_value: str, confidence=1.0):
        tag = Label(tag_value, confidence)
        self.tags[tag_type] = tag

    def get_tag(self, tag_type: str) -> Label:
        if tag_type in self.tags: return self.tags[tag_type]
        return Label('')

    def get_head(self):
        return self.sentence.get_token(self.head_id)

    def set_embedding(self, name: str, vector: torch.autograd.Variable):
        self._embeddings[name] = vector.cpu()

    def clear_embeddings(self):
        self._embeddings: Dict = {}

    def get_embedding(self) -> torch.tensor:
        embeddings = [self._embeddings[embed] for embed in sorted(self._embeddings.keys())]

        if embeddings:
            return torch.cat(embeddings, dim=0)

        return torch.Tensor()

    @property
    def start_position(self) -> int:
        return self.start_pos

    @property
    def end_position(self) -> int:
        return self.end_pos

    @property
    def embedding(self):
        return self.get_embedding()

    def __str__(self) -> str:
        return 'Token: {} {}'.format(self.idx, self.text) if self.idx is not None else 'Token: {}'.format(self.text)

    def __repr__(self) -> str:
        return 'Token: {} {}'.format(self.idx, self.text) if self.idx is not None else 'Token: {}'.format(self.text)


class Span:
    """
    This class represents one textual span consisting of Tokens. A span may have a tag.
    """

    def __init__(self, tokens: List[Token], tag: str = None, score=1.):
        self.tokens = tokens
        self.tag = tag
        self.score = score
        self.start_pos = None
        self.end_pos = None

        if tokens:
            self.start_pos = tokens[0].start_position
            self.end_pos = tokens[len(tokens) - 1].end_position

    @property
    def text(self) -> str:
        return ' '.join([t.text for t in self.tokens])

    def to_original_text(self) -> str:
        str = ''
        pos = self.tokens[0].start_pos
        for t in self.tokens:
            while t.start_pos != pos:
                str += ' '
                pos += 1

            str += t.text
            pos += len(t.text)

        return str

    def to_dict(self):
        return {
            'text': self.to_original_text(),
            'start_pos': self.start_pos,
            'end_pos': self.end_pos,
            'type': self.tag,
            'confidence': self.score
        }

    def __str__(self) -> str:
        ids = ','.join([str(t.idx) for t in self.tokens])
        return '{}-span [{}]: "{}"'.format(self.tag, ids, self.text) \
            if self.tag is not None else 'span [{}]: "{}"'.format(ids, self.text)

    def __repr__(self) -> str:
        ids = ','.join([str(t.idx) for t in self.tokens])
        return '<{}-span ({}): "{}">'.format(self.tag, ids, self.text) \
            if self.tag is not None else '<span ({}): "{}">'.format(ids, self.text)


class Sentence:
    """
    A Sentence is a list of Tokens and is used to represent a sentence or text fragment.
    """

    def __init__(self, text: str = None, use_tokenizer: bool = False, labels: Union[List[Label], List[str]] = None):

        super(Sentence, self).__init__()

        self.tokens: List[Token] = []

        self.labels: List[Label] = []
        if labels is not None: self.add_labels(labels)

        self._embeddings: Dict = {}

        # if text is passed, instantiate sentence with tokens (words)
        if text is not None:

            # tokenize the text first if option selected
            if use_tokenizer:

                # use segtok for tokenization
                tokens = []
                sentences = split_single(text)
                for sentence in sentences:
                    contractions = split_contractions(word_tokenizer(sentence))
                    tokens.extend(contractions)

                # determine offsets for whitespace_after field
                index = text.index
                running_offset = 0
                last_word_offset = -1
                last_token = None
                for word in tokens:
                    try:
                        word_offset = index(word, running_offset)
                        start_position = word_offset
                    except:
                        word_offset = last_word_offset + 1
                        start_position = running_offset + 1 if running_offset > 0 else running_offset

                    token = Token(word, start_position=start_position)
                    self.add_token(token)

                    if word_offset - 1 == last_word_offset and last_token is not None:
                        last_token.whitespace_after = False

                    word_len = len(word)
                    running_offset = word_offset + word_len
                    last_word_offset = running_offset - 1
                    last_token = token

            # otherwise assumes whitespace tokenized text
            else:
                # catch the empty string case
                if not text:
                    raise ValueError("Cannot convert empty string to a Sentence object.")
                # add each word in tokenized string as Token object to Sentence
                word = ''
                for index, char in enumerate(text):
                    if char == ' ':
                        if len(word) > 0:
                            token = Token(word, start_position=index-len(word))
                            self.add_token(token)

                        word = ''
                    else:
                        word += char
                # increment for last token in sentence if not followed by whtespace
                index += 1
                if len(word) > 0:
                    token = Token(word, start_position=index-len(word))
                    self.add_token(token)

    def get_token(self, token_id: int) -> Token:
        for token in self.tokens:
            if token.idx == token_id:
                return token

    def add_token(self, token: Token):
        self.tokens.append(token)

        # set token idx if not set
        token.sentence = self
        if token.idx is None:
            token.idx = len(self.tokens)

    def get_spans(self, tag_type: str, min_score=-1) -> List[Span]:

        spans: List[Span] = []

        current_span = []

        tags = defaultdict(lambda: 0.0)

        previous_tag_value: str = 'O'
        for token in self:

            tag: Label = token.get_tag(tag_type)
            tag_value = tag.value

            # non-set tags are OUT tags
            if tag_value == '' or tag_value == 'O':
                tag_value = 'O-'

            # anything that is not a BIOES tag is a SINGLE tag
            if tag_value[0:2] not in ['B-', 'I-', 'O-', 'E-', 'S-']:
                tag_value = 'S-' + tag_value

            # anything that is not OUT is IN
            in_span = False
            if tag_value[0:2] not in ['O-']:
                in_span = True

            # single and begin tags start a new span
            starts_new_span = False
            if tag_value[0:2] in ['B-', 'S-']:
                starts_new_span = True

            if previous_tag_value[0:2] in ['S-'] and previous_tag_value[2:] != tag_value[2:] and in_span:
                starts_new_span = True

            if (starts_new_span or not in_span) and len(current_span) > 0:
                scores = [t.get_tag(tag_type).score for t in current_span]
                span_score = sum(scores) / len(scores)
                if span_score > min_score:
                    spans.append(Span(
                        current_span,
                        tag=sorted(tags.items(), key=lambda k_v: k_v[1], reverse=True)[0][0],
                        score=span_score)
                    )
                current_span = []
                tags = defaultdict(lambda: 0.0)

            if in_span:
                current_span.append(token)
                weight = 1.1 if starts_new_span else 1.0
                tags[tag_value[2:]] += weight

            # remember previous tag
            previous_tag_value = tag_value

        if len(current_span) > 0:
            scores = [t.get_tag(tag_type).score for t in current_span]
            span_score = sum(scores) / len(scores)
            if span_score > min_score:
                spans.append(Span(
                    current_span,
                    tag=sorted(tags.items(), key=lambda k_v: k_v[1], reverse=True)[0][0],
                    score=span_score)
                )

        return spans

    def add_label(self, label: Union[Label, str]):
        if type(label) is Label:
            self.labels.append(label)

        elif type(label) is str:
            self.labels.append(Label(label))

    def add_labels(self, labels: Union[List[Label], List[str]]):
        for label in labels:
            self.add_label(label)

    def get_label_names(self) -> List[str]:
        return [label.value for label in self.labels]

    @property
    def embedding(self):
        return self.get_embedding()

    def set_embedding(self, name: str, vector):
        self._embeddings[name] = vector.cpu()

    def get_embedding(self) -> torch.tensor:
        embeddings = []
        for embed in sorted(self._embeddings.keys()):
            embedding = self._embeddings[embed]
            embeddings.append(embedding)

        if embeddings:
            return torch.cat(embeddings, dim=0)

        return torch.Tensor()

    def clear_embeddings(self, also_clear_word_embeddings: bool = True):
        self._embeddings: Dict = {}

        if also_clear_word_embeddings:
            for token in self:
                token.clear_embeddings()

    def cpu_embeddings(self):
        for name, vector in self._embeddings.items():
            self._embeddings[name] = vector.cpu()

    def to_tagged_string(self, main_tag=None) -> str:
        list = []
        for token in self.tokens:
            list.append(token.text)

            tags: List[str] = []
            for tag_type in token.tags.keys():

                if main_tag is not None and main_tag != tag_type: continue

                if token.get_tag(tag_type).value == '' or token.get_tag(tag_type).value == 'O': continue
                tags.append(token.get_tag(tag_type).value)
            all_tags = '<' + '/'.join(tags) + '>'
            if all_tags != '<>':
                list.append(all_tags)
        return ' '.join(list)

    def to_tokenized_string(self) -> str:
        return ' '.join([t.text for t in self.tokens])

    def to_plain_string(self):
        plain = ''
        for token in self.tokens:
            plain += token.text
            if token.whitespace_after: plain += ' '
        return plain.rstrip()

    def convert_tag_scheme(self, tag_type: str = 'ner', target_scheme: str = 'iob'):

        tags: List[Label] = []
        for token in self.tokens:
            token: Token = token
            tags.append(token.get_tag(tag_type))

        if target_scheme == 'iob':
            iob2(tags)

        if target_scheme == 'iobes':
            iob2(tags)
            tags = iob_iobes(tags)

        for index, tag in enumerate(tags):
            self.tokens[index].add_tag(tag_type, tag)

    def infer_space_after(self):
        """
        Heuristics in case you wish to infer whitespace_after values for tokenized text. This is useful for some old NLP
        tasks (such as CoNLL-03 and CoNLL-2000) that provide only tokenized data with no info of original whitespacing.
        :return:
        """
        last_token = None
        quote_count: int = 0
        # infer whitespace after field

        for token in self.tokens:
            if token.text == '"':
                quote_count += 1
                if quote_count % 2 != 0:
                    token.whitespace_after = False
                elif last_token is not None:
                    last_token.whitespace_after = False

            if last_token is not None:

                if token.text in ['.', ':', ',', ';', ')', 'n\'t', '!', '?']:
                    last_token.whitespace_after = False

                if token.text.startswith('\''):
                    last_token.whitespace_after = False

            if token.text in ['(']:
                token.whitespace_after = False

            last_token = token
        return self

    def to_original_text(self) -> str:
        str = ''
        pos = 0
        for t in self.tokens:
            while t.start_pos != pos:
                str += ' '
                pos += 1

            str += t.text
            pos += len(t.text)

        return str

    def to_dict(self, tag_type: str = None):
        labels = []
        entities = []

        if tag_type:
            entities = [span.to_dict() for span in self.get_spans(tag_type)]
        if self.labels:
            labels = [l.to_dict() for l in self.labels]

        return {
            'text': self.to_original_text(),
            'labels': labels,
            'entities': entities
        }

    def __getitem__(self, idx: int) -> Token:
        return self.tokens[idx]

    def __iter__(self):
        return iter(self.tokens)

    def __repr__(self):
        return 'Sentence: "{}" - {} Tokens'.format(' '.join([t.text for t in self.tokens]), len(self))

    def __copy__(self):
        s = Sentence()
        for token in self.tokens:
            nt = Token(token.text)
            for tag_type in token.tags:
                nt.add_tag(tag_type, token.get_tag(tag_type).value, token.get_tag(tag_type).score)

            s.add_token(nt)
        return s

    def __str__(self) -> str:

        if self.labels:
            return f'Sentence: "{self.to_tokenized_string()}" - {len(self)} Tokens - Labels: {self.labels} '
        else:
            return f'Sentence: "{self.to_tokenized_string()}" - {len(self)} Tokens'

    def __len__(self) -> int:
        return len(self.tokens)


class Corpus:

    @abstractmethod
    def train(self) -> Iterable[Sentence]:
        pass

    @abstractmethod
    def dev(self) -> Iterable[Sentence]:
        pass

    @abstractmethod
    def test(self) -> Iterable[Sentence]:
        pass

    @abstractmethod
    def downsample(self, percentage: float = 0.1, only_downsample_train=False):
        """Downsamples this corpus to a percentage of the sentences."""
        pass

    @abstractmethod
    def get_all_sentences(self) -> Iterable[Sentence]:
        """Gets all sentences in the corpus (train, dev and test splits together)."""
        pass

    @abstractmethod
    def make_tag_dictionary(self, tag_type: str) -> Dictionary:
        """Produces a dictionary of token tags of tag_type."""
        pass

    @abstractmethod
    def make_label_dictionary(self) -> Dictionary:
        """
        Creates a dictionary of all labels assigned to the sentences in the corpus.
        :return: dictionary of labels
        """
        pass


def wrapper(obj):
        if callable(obj):
            return obj()
        else:
            return iter(obj)


class TaggedCorpus(Corpus):
    def __init__(self, train: Iterable[Sentence], dev: Iterable[Sentence], test: Iterable[Sentence], name: str = 'corpus'):
        self._train: Iterable[Sentence] = train
        self._dev: Iterable[Sentence] = dev
        self._test: Iterable[Sentence] = test
        self.name: str = name

    def train(self) -> Iterable[Sentence]:
        return wrapper(self._train)

    def dev(self) -> Iterable[Sentence]:
        return wrapper(self._dev)

    def test(self) -> Iterable[Sentence]:
        return wrapper(self._test)

    def downsample(self, percentage: float = 0.1, only_downsample_train=False):

        self._train = self._downsample_to_proportion(self.train(), percentage)
        if not only_downsample_train:
            self._dev = self._downsample_to_proportion(self.dev(), percentage)
            self._test = self._downsample_to_proportion(self.test(), percentage)

        return self

    def get_all_sentences(self) -> Iterable[Sentence]:
        return itertools.chain(self.train(), self.dev(), self.test())

    def make_tag_dictionary(self, tag_type: str) -> Dictionary:

        # Make the tag dictionary
        tag_dictionary: Dictionary = Dictionary()
        tag_dictionary.add_item('O')
        for sentence in self.get_all_sentences():
            for token in sentence.tokens:
                token: Token = token
                tag_dictionary.add_item(token.get_tag(tag_type).value)
        tag_dictionary.add_item('<START>')
        tag_dictionary.add_item('<STOP>')
        return tag_dictionary

    def make_label_dictionary(self) -> Dictionary:
        """
        Creates a dictionary of all labels assigned to the sentences in the corpus.
        :return: dictionary of labels
        """

        labels = set(self._get_all_label_names())

        label_dictionary: Dictionary = Dictionary(add_unk=False)
        for label in labels:
            label_dictionary.add_item(label)

        return label_dictionary

    def make_vocab_dictionary(self, max_tokens=-1, min_freq=1) -> Dictionary:
        """
        Creates a dictionary of all tokens contained in the corpus.
        By defining `max_tokens` you can set the maximum number of tokens that should be contained in the dictionary.
        If there are more than `max_tokens` tokens in the corpus, the most frequent tokens are added first.
        If `min_freq` is set the a value greater than 1 only tokens occurring more than `min_freq` times are considered
        to be added to the dictionary.
        :param max_tokens: the maximum number of tokens that should be added to the dictionary (-1 = take all tokens)
        :param min_freq: a token needs to occur at least `min_freq` times to be added to the dictionary (-1 = there is no limitation)
        :return: dictionary of tokens
        """
        tokens = self._get_most_common_tokens(max_tokens, min_freq)

        vocab_dictionary: Dictionary = Dictionary()
        for token in tokens:
            vocab_dictionary.add_item(token)

        return vocab_dictionary

    def _get_most_common_tokens(self, max_tokens, min_freq) -> List[str]:
        tokens_and_frequencies = Counter(self._get_all_tokens())
        tokens_and_frequencies = tokens_and_frequencies.most_common()

        tokens = []
        for token, freq in tokens_and_frequencies:
            if (min_freq != -1 and freq < min_freq) or (max_tokens != -1 and len(tokens) == max_tokens):
                break
            tokens.append(token)
        return tokens

    def _get_all_label_names(self) -> List[str]:
        return [label.value for sent in self.train() for label in sent.labels]

    def _get_all_tokens(self) -> List[str]:
        tokens = list(map((lambda s: s.tokens), self.train()))
        tokens = [token for sublist in tokens for token in sublist]
        return list(map((lambda t: t.text), tokens))

    def _downsample_to_proportion(self, list: Iterable, proportion: float):

        counter = 0.0
        last_counter = None
        downsampled: List = []

        for item in list:
            counter += proportion
            if int(counter) != last_counter:
                downsampled.append(item)
                last_counter = int(counter)
        return downsampled

    def obtain_statistics(self, tag_type: str = None, pretty_print: bool = True) -> dict:
        """
        Print statistics about the class distribution (only labels of sentences are taken into account) and sentence
        sizes.
        """
        json_string = {
            "TRAIN": self._obtain_statistics_for(self.train, "TRAIN", tag_type),
            "TEST": self._obtain_statistics_for(self.test, "TEST", tag_type),
            "DEV": self._obtain_statistics_for(self.dev, "DEV", tag_type),
        }
        if pretty_print:
            import json
            json_string = json.dumps(json_string, indent=4)
        return json_string

    @staticmethod
    def _obtain_statistics_for(sentences, name, tag_type) -> dict:
        if len(sentences) == 0:
            return {}

        classes_to_count = TaggedCorpus._get_class_to_count(sentences)
        tags_to_count = TaggedCorpus._get_tag_to_count(sentences, tag_type)
        tokens_per_sentence = TaggedCorpus._get_tokens_per_sentence(sentences)

        label_size_dict = {}
        for l, c in classes_to_count.items():
            label_size_dict[l] = c

        tag_size_dict = {}
        for l, c in tags_to_count.items():
            tag_size_dict[l] = c

        return {
            'dataset': name,
            'total_number_of_documents': len(sentences),
            'number_of_documents_per_class': label_size_dict,
            'number_of_tokens_per_tag': tag_size_dict,
            'number_of_tokens': {
                'total': sum(tokens_per_sentence),
                'min': min(tokens_per_sentence),
                'max': max(tokens_per_sentence),
                'avg': sum(tokens_per_sentence) / len(sentences)
            }
        }

    @staticmethod
    def _get_tokens_per_sentence(sentences):
        return list(map(lambda x: len(x.tokens), sentences))

    @staticmethod
    def _get_class_to_count(sentences):
        class_to_count = defaultdict(lambda: 0)
        for sent in sentences:
            for label in sent.labels:
                class_to_count[label.value] += 1
        return class_to_count

    @staticmethod
    def _get_tag_to_count(sentences, tag_type):
        tag_to_count = defaultdict(lambda: 0)
        for sent in sentences:
            for word in sent.tokens:
                if tag_type in word.tags:
                    label = word.tags[tag_type]
                    tag_to_count[label.value] += 1
        return tag_to_count

    def __str__(self) -> str:
        return 'TaggedCorpus: %d train + %d dev + %d test sentences' % (len(self.train), len(self.dev), len(self.test))


def iob2(tags):
    """
    Check that tags have a valid IOB format.
    Tags in IOB1 format are converted to IOB2.
    """
    for i, tag in enumerate(tags):
        if tag.value == 'O':
            continue
        split = tag.value.split('-')
        if len(split) != 2 or split[0] not in ['I', 'B']:
            return False
        if split[0] == 'B':
            continue
        elif i == 0 or tags[i - 1].value == 'O':  # conversion IOB1 to IOB2
            tags[i].value = 'B' + tag.value[1:]
        elif tags[i - 1].value[1:] == tag.value[1:]:
            continue
        else:  # conversion IOB1 to IOB2
            tags[i].value = 'B' + tag.value[1:]
    return True


def iob_iobes(tags):
    """
    IOB -> IOBES
    """
    new_tags = []
    for i, tag in enumerate(tags):
        if tag.value == 'O':
            new_tags.append(tag.value)
        elif tag.value.split('-')[0] == 'B':
            if i + 1 != len(tags) and \
                    tags[i + 1].value.split('-')[0] == 'I':
                new_tags.append(tag.value)
            else:
                new_tags.append(tag.value.replace('B-', 'S-'))
        elif tag.value.split('-')[0] == 'I':
            if i + 1 < len(tags) and \
                    tags[i + 1].value.split('-')[0] == 'I':
                new_tags.append(tag.value)
            else:
                new_tags.append(tag.value.replace('I-', 'E-'))
        else:
            raise Exception('Invalid IOB format!')
    return new_tags


class MultiCorpus(Corpus):

    def __init__(self, corpora: List[TaggedCorpus]):
        self.corpora: List[TaggedCorpus] = corpora

    def train(self) -> Iterable[Sentence]:
        iters = []
        for corpus in self.corpora:
            iters.append(corpus.train())
        return itertools.chain(*iters)

    def dev(self) -> Iterable[Sentence]:
        iters = []
        for corpus in self.corpora:
            iters.append(corpus.dev())
        return itertools.chain(*iters)

    def test(self) -> Iterable[Sentence]:
        iters = []
        for corpus in self.corpora:
            iters.append(corpus.test())
        return itertools.chain(*iters)

    def __str__(self):
        return '\n'.join([str(corpus) for corpus in self.corpora])

    def get_all_sentences(self) -> List[Sentence]:
        iters = []
        for corpus in self.corpora:
            iters.append(corpus.get_all_sentences())
        return itertools.chain(*iters)

    def downsample(self, percentage: float = 0.1, only_downsample_train=False):

        for corpus in self.corpora:
            corpus.downsample(percentage, only_downsample_train)

        return self

    def make_tag_dictionary(self, tag_type: str) -> Dictionary:

        # Make the tag dictionary
        tag_dictionary: Dictionary = Dictionary()
        tag_dictionary.add_item('O')
        for corpus in self.corpora:
            for sentence in corpus.get_all_sentences():
                for token in sentence.tokens:
                    token: Token = token
                    tag_dictionary.add_item(token.get_tag(tag_type).value)
        tag_dictionary.add_item('<START>')
        tag_dictionary.add_item('<STOP>')
        return tag_dictionary

    def make_label_dictionary(self) -> Dictionary:

        label_dictionary: Dictionary = Dictionary(add_unk=False)
        for corpus in self.corpora:
            labels = set(corpus._get_all_label_names())

            for label in labels:
                label_dictionary.add_item(label)

        return label_dictionary
