#!/usr/bin/env python2.7

from __future__ import print_function
from __future__ import division

import numpy
import theano
import pickle
from theano import tensor as T
from copy import deepcopy
from Log import log
from LmDataset import Lexicon, StateTying
from os.path import isfile


class Edge:
  """
  class to represent an edge
  """

  # label placeholder
  SIL = '_'
  EPS = '*'
  BLANK = '%'

  def __init__(self, source_state_idx, target_state_idx, label, weight=0.0):
    """
    :param int source_state_idx: the starting node of the edge
    :param int target_state_idx: the ending node od th edge
    :param int|str|None label: the label of the edge (normally a letter or a phoneme ...)
    :param float weight: probability of the word/phon in -log space
    """
    self.source_state_idx = source_state_idx
    self.target_state_idx = target_state_idx
    self.label = label
    self.weight = weight

    # int|str|None label_prev: previous label
    self.label_prev = None
    # int|str|None label_next: next label
    self.label_next = None
    # int|None idx_word_in_sentence: index of word in the given sentence
    self.idx_word_in_sentence = None
    # int|None idx_phon_in_word: index of phon in a word
    self.idx_phon_in_word = None
    # int|None idx: label index within the sentence/word/phon
    self.idx = None
    # int|None allo_idx: allophone position
    self.allo_idx = None
    # bool phon_at_word_begin: flag indicates if phon at the beginning of a word
    self.phon_at_word_begin = False
    # bool phon_at_word_end: flag indicates if phon at the end of a word
    self.phon_at_word_end = False
    # float|None score: score of the edge
    self.score = None
    # bool is_loop: is the edge a loop within the graph
    self.is_loop = False

  def __repr__(self):
    return "".join(("[",
                    str(self.source_state_idx), ", ",
                    str(self.target_state_idx), ", ",
                    str(self.label), ", ",
                    str(self.weight),
                    "]"))

  def __str__(self):
    return "".join(("Edge:\n",
                    "Source state: ",
                    str(self.source_state_idx), "\n",
                    "Target state: ",
                    str(self.target_state_idx), "\n",
                    "Label: ",
                    str(self.label), "\n",
                    "Weight: ",
                    str(self.weight)))

  def as_tuple(self):
    return self.source_state_idx, self.target_state_idx, self.label, self.weight

  def __eq__(self, other):
    return self.as_tuple() == other.as_tuple()

  def __ne__(self, other):
    return self.as_tuple() != other.as_tuple()

  def __le__(self, other):
    return self.as_tuple() <= other.as_tuple()

  def __lt__(self, other):
    return self.as_tuple() < other.as_tuple()

  def __ge__(self, other):
    return self.as_tuple() >= other.as_tuple()

  def __gt__(self, other):
    return self.as_tuple() > other.as_tuple()


class Graph:
  """
  class holds the Graph representing the Finite State Automaton
  holds the input and the created output (ASG, CTC, HMM)
  states between input and output may be held if necessary
  """

  def __init__(self, lemma):
    """
    :param str|None lemma: a sentence or word
    list[str] lem_list: lemma transformed into list of strings
    """
    if isinstance(lemma, str):
      self.lemma = lemma.strip()
      self.lem_list = self.lemma.lower().split()
    elif isinstance(lemma, list):
      self.lemma = None
      self.lem_list = lemma
    else:
      assert False, ("The input you provided is not acceptable!", lemma)

    self.filename = None
    # int num_states: number of states of FSA during creation and final
    self.num_states = -1
    self.num_states_asg = -1
    self.num_states_ctc = -1
    self.num_states_hmm = -1
    self.num_states_word = -1
    # list[Edge] edges: edges of FSA during creation and final state
    self.edges = []
    self.edges_asg = []
    self.edges_ctc = []
    self.edges_hmm = []
    self.edges_word = []

  def __repr__(self):
    return "Graph()"

  def __str__(self):
    prettygraph = "Graph:\n"\
                  + str(self.lem_list)\
                  + "\nASG:\nNum states: "\
                  + str(self.num_states_asg)\
                  + "\nEdges:\n"\
                  + str(self.edges_asg)\
                  + "\nCTC:\nNum states: "\
                  + str(self.num_states_ctc)\
                  + "\nEdges:\n"\
                  + str(self.edges_ctc)\
                  + "\nHMM:\nNum states: "\
                  + str(self.num_states_hmm)\
                  + "\nEdges:\n"\
                  + str(self.edges_hmm)
    return prettygraph

  @staticmethod
  def make_single_state_graph(num_states, edges):
    """
    takes a graph with several states and transforms into single state graph
    :param int num_states: number of states
    :param list[Edges] edges: list of Edges symbolizing the graph
    :return: returns the transformed list of Edges with one state
    :rtype: list[Edges]
    """
    edges_single_state = deepcopy(edges)
    if num_states > 1:
      for edge in edges_single_state:
        edge.source_state_idx = 0
        edge.target_state_idx = 0

    return edges_single_state


class Asg:
  """
  class to create ASG FSA
  """

  def __init__(self, fsa, num_labels=256, asg_repetition=2, label_conversion=False):
    """
    :param Graph fsa: represents the Graph on which the class operates
    :param int num_labels: number of labels without blank, silence, eps and repetitions
      where num_labels > 0
    :param int asg_repetition: asg repeat symbol which stands for x repetitions
      where asg_repetition > 1
    :param bool label_conversion: shall the labels be converted into numbers (only ASG and CTC)
    """
    if isinstance(fsa, Graph) and isinstance(num_labels, int)\
       and isinstance(asg_repetition, int) and isinstance(label_conversion, bool):
      self.fsa = fsa
      self.num_labels = num_labels
      self.asg_repetition = asg_repetition
      self.label_conversion = label_conversion
      self.separator = False  # words in the sentence will be separated by Edge.BLANK
    else:
      assert False, ("The ASG init went wrong!", fsa)

  def run(self):
    """
    creates the ASG FSA
    """
    print("Starting ASG FSA Creation")
    label_prev = None
    rep_count = 0
    label_repetitions = []  # marks the labels which will be replaced with a rep symbol

    # goes through the list of strings
    for lem in self.fsa.lem_list:
      # goes through the string
      reps_label = []
      for label in lem:
        label_cur = label
        # check if current label matches previous label and generates label reps list
        if label_cur == label_prev:
          # adds reps symbol
          if rep_count < self.asg_repetition:
            rep_count += 1
          else:
            reps_label.append(self.num_labels + rep_count)
            rep_count = 1
        else:
          # adds normal label
          if rep_count != 0:
            reps_label.append(self.num_labels + rep_count)
            rep_count = 0
          reps_label.append(label)
        label_prev = label
      # put reps list back into list -> list[list[str|int]]
      label_repetitions.append(reps_label)

    # create states
    self.fsa.num_states = 0
    cur_idx = 0
    src_idx = 0
    trgt_idx = 0
    for rep_index, rep_label in enumerate(label_repetitions):
      for idx, lab in enumerate(rep_label):
        src_idx = cur_idx
        trgt_idx = src_idx + 1
        if cur_idx == 0:  # for final state
          self.fsa.num_states += 1
        self.fsa.num_states += 1
        edge = Edge(src_idx, trgt_idx, lab)
        edge.idx_word_in_sentence = rep_index
        edge.idx_phon_in_word = idx
        edge.idx = cur_idx
        if idx == 0:
          edge.phon_at_word_begin = True
        if idx == len(rep_label) - 1:
          edge.phon_at_word_end = True
        self.fsa.edges.append(edge)
        cur_idx += 1
      # adds separator between words in sentence
      if self.separator and rep_index < len(label_repetitions) - 1:
        self.fsa.edges.append(Edge(src_idx + 1, trgt_idx + 1, Edge.BLANK))
        self.fsa.num_states += 1
        cur_idx += 1

    # adds loops to ASG FSA
    for loop_idx in range(1, self.fsa.num_states):
      edges_add_loop = [edge_idx for edge_idx, edge_cur in enumerate(self.fsa.edges)
                        if (edge_cur.target_state_idx == loop_idx and edge_cur.label != Edge.EPS
                            and edge_cur.label != Edge.SIL)]
      for add_loop_edge in edges_add_loop:
        edge = deepcopy(self.fsa.edges[add_loop_edge])
        edge.source_state_idx = edge.target_state_idx
        edge.is_loop = True
        self.fsa.edges.append(edge)

    self.fsa.edges.sort()

    # label conversion
    if self.label_conversion:
      Store.label_conversion(self.fsa.edges)

    self.fsa.num_states_asg = deepcopy(self.fsa.num_states)
    self.fsa.num_states = -1
    self.fsa.edges_asg = deepcopy(self.fsa.edges)
    self.fsa.edges = []


class Ctc:
  """
  class to create CTC FSA
  """

  def __init__(self, fsa, num_labels=256, label_conversion=False):
    """
    :param Graph fsa: represents the Graph on which the class operates
    :param int num_labels: number of labels without blank, silence, eps and repetitions
    :param bool label_conversion: shall the labels be converted into numbers (only ASG and CTC)
    """
    assert isinstance(fsa, Graph)
    assert isinstance(num_labels, int)
    assert isinstance(label_conversion, int)
    self.fsa = fsa
    self.num_labels = num_labels
    self.label_conversion = label_conversion

    # list[int] final_states: list of final states
    self.final_states = []

  def run(self):
    """
    creates the CTC FSA
    """
    print("Starting CTC FSA Creation")
    self.fsa.num_states = 0
    cur_idx = 0

    # goes through the list of strings
    for idx, seq in enumerate(self.fsa.lem_list):
      # goes through string
      for i, label in enumerate(seq):
        src_idx = 2 * cur_idx
        if cur_idx == 0:
          self.fsa.num_states += 1
        trgt_idx = src_idx + 2
        e_norm = Edge(src_idx, trgt_idx, seq[i])
        e_norm.idx = cur_idx
        e_norm.idx_word_in_sentence = idx
        e_norm.idx_phon_in_word = i
        # if two equal labels back to back in string -> skip repetition
        if seq[i] != seq[i - 1] or len(seq) == 1:
          self.fsa.edges.append(e_norm)
        # adds blank labels and label repetitions
        e_blank = Edge(src_idx, trgt_idx - 1, Edge.BLANK)
        self.fsa.edges.append(e_blank)
        e_rep = deepcopy(e_norm)
        e_rep.source_state_idx = src_idx + 1
        self.fsa.edges.append(e_rep)
        cur_idx += 1
        # add number of states
        self.fsa.num_states += 2

      # adds separator between words in sentence
      if idx < len(self.fsa.lem_list) - 1:
        self.fsa.edges.append(Edge(2 * cur_idx, 2 * cur_idx + 1, Edge.BLANK))
        self.fsa.edges.append(Edge(2 * cur_idx + 1, 2 * cur_idx + 2, Edge.SIL))
        self.fsa.edges.append(Edge(2 * cur_idx, 2 * cur_idx + 2, Edge.SIL))
        self.fsa.num_states += 2
        cur_idx += 1

    # add node number of final state
    self.final_states.append(self.fsa.num_states - 1)

    # add all final possibilities
    e_end_1 = Edge(self.fsa.num_states - 3, self.fsa.num_states, Edge.BLANK, 1.)
    self.fsa.edges.append(e_end_1)
    e_end_2 = Edge(self.fsa.num_states + 1, self.fsa.num_states + 2, Edge.BLANK, 1.)
    self.fsa.edges.append(e_end_2)
    e_end_3 = Edge(self.fsa.num_states, self.fsa.num_states + 1, self.fsa.lem_list[-1][-1], 1.)
    self.fsa.edges.append(e_end_3)
    self.fsa.num_states += 3
    # add node nuber of final state
    self.final_states.append(self.fsa.num_states - 1)

    # make single final node
    if not (len(self.final_states) == 1 and self.final_states[0] == self.fsa.num_states - 1):
      # add new single final node
      self.fsa.num_states += 1
      for fstate in self.final_states:
        # find edges which end in final nodes
        final_state_idx_list = [edge_idx for edge_idx, edge in enumerate(self.fsa.edges)
                                if edge.target_state_idx == fstate]
        # add edge from final nodes to new single final node
        final_state_node = Edge(fstate, self.fsa.num_states - 1,
                                self.fsa.edges[final_state_idx_list[0]].label)
        self.fsa.edges.append(final_state_node)
        for final_state_idx in final_state_idx_list:
          # add edges from nodes which go to final nodes
          final_state_edge = deepcopy(self.fsa.edges[final_state_idx])
          final_state_edge.target_state_idx = self.fsa.num_states - 1
          self.fsa.edges.append(final_state_edge)

    # add loops to CTC FSA
    for loop_idx in range(1, self.fsa.num_states - 1):
      edges_add_loop = [edge_idx for edge_idx, edge_cur in enumerate(self.fsa.edges)
                        if (edge_cur.target_state_idx == loop_idx)]
      edge = deepcopy(self.fsa.edges[edges_add_loop[0]])
      edge.source_state_idx = edge.target_state_idx
      edge.is_loop = True
      self.fsa.edges.append(edge)

    # label conversion
    if self.label_conversion:
      Store.label_conversion(self.fsa.edges)

    self.fsa.edges.sort()
    self.fsa.num_states_ctc = deepcopy(self.fsa.num_states)
    self.fsa.num_states = -1
    self.fsa.edges_ctc = deepcopy(self.fsa.edges)
    self.fsa.edges = []


class Hmm:
  """
  class to create HMM FSA
  """

  def __init__(self, fsa, depth=6, allo_num_states=3, state_tying_conversion=False):
    """
    :param Graph fsa: represents the Graph on which the class operates
    :param int depth: the depth of the HMM FSA process
    :param int allo_num_states: number of allophone states
      where allo_num_states > 0
    :param bool state_tying_conversion: flag for state tying conversion
    """
    if isinstance(fsa, Graph) and isinstance(depth, int) and isinstance(allo_num_states, int):
      self.fsa = fsa
      self.depth = depth
      self.allo_num_states = allo_num_states
      self.state_tying_conversion = state_tying_conversion
    else:
      assert False, ('The HMM init went wrong', fsa)

    # Lexicon|None lexicon: lexicon for transforming a word into allophones
    self.lexicon = None
    # StateTying|None state_tying: holds the transformation from created label to number
    self.state_tying = None
    # dict phon_dict: dictionary of phonemes, loaded from lexicon file
    self.phon_dict = {}

  @staticmethod
  def _find_node_in_edges(node, edges):
    """
    find a specific node in all edges
    :param int node: node number
    :param list edges: all edges
    :return node_dict: dict of nodes where
          key: edge index
          value: 0 = specific node is as source state idx
          value: 1 = specific node is target state idx
          value: 2 = specific node is source and target state idx
    :rtype: dict
    """
    node_dict = {}

    pos_start = [edge_index for edge_index, edge in enumerate(edges)
                 if (edge.source_state_idx == node)]
    pos_end = [edge_index for edge_index, edge in enumerate(edges)
               if (edge.target_state_idx == node)]
    pos_start_end = [edge_index for edge_index, edge in enumerate(edges) if
                     (edge.source_state_idx == node and edge.target_state_idx == node)]

    for pos in pos_start:
      node_dict[pos] = 0

    for pos in pos_end:
      node_dict[pos] = 1

    for pos in pos_start_end:
      node_dict[pos] = 2

    return node_dict

  @staticmethod
  def _build_allo_syntax_for_mapping(edge):
    """
    builds a conforming allo syntax for mapping
    :param Edge edge: edge to build the allo syntax from
    :return allo_map: a allo syntax ready for mapping
    :rtype: str
    """
    if edge.label == Edge.SIL:
      allo_map = "%s{#+#}" % '[SILENCE]'
    elif edge.label == Edge.EPS:
      allo_map = "*"
    else:
      if edge.label_prev == '' and edge.label_next == '':
        allo_map = "%s{#+#}" % edge.label
      elif edge.label_prev == '':
        allo_map = "%s{#+%s}" % (edge.label, edge.label_next)
      elif edge.label_next == '':
        allo_map = "%s{%s+#}" % (edge.label, edge.label_prev)
      else:
        allo_map = "%s{%s+%s}" % (edge.label, edge.label_prev, edge.label_next)

    if edge.phon_at_word_begin:
      allo_map += '@i'
    if edge.phon_at_word_end:
      allo_map += '@f'

    if edge.label == Edge.SIL:
      allo_map += ".0"
    elif edge.label == Edge.EPS:
      allo_map += ""
    elif edge.allo_idx is not None:
      allo_map += "." + str(edge.allo_idx)

    return allo_map

  def run(self):
    """
    creates the HMM FSA
    """
    print("Starting HMM FSA Creation")
    self.fsa.num_states_hmm = 0
    split_node = 0
    merge_node = 0

    for word_idx, word in enumerate(self.fsa.lem_list):
      if word_idx == 0:
        # add first silence and eps
        self.fsa.edges.append(Edge(0, 1, Edge.SIL))
        self.fsa.edges.append(Edge(0, 1, Edge.EPS))
        self.fsa.num_states += 2
      # get word with phons from lexicon
      self.phon_dict[word] = self.lexicon.lemmas[word]['phons']
      # go through all phoneme variations for a given word
      for lemma_idx, lemma in enumerate(self.phon_dict[word]):
        # go through the phoneme variations phoneme by phoneme
        lem = lemma['phon'].split(' ')
        phon_dict_len = len(self.phon_dict[word])
        for phon_idx, phon in enumerate(lem):
          if phon_dict_len == 1:
            # only one phoneme variation - no split!!!
            source_node = self.fsa.num_states
            target_node = self.fsa.num_states + 1
            self.fsa.num_states += 1
          else:
            # several phoneme variations - split and merge required
            if lemma_idx == 0:
              # save split node
              if phon_idx == 0:
                split_node = self.fsa.num_states
              # save merge node
              if phon_idx == len(lem) - 1:
                merge_node = self.fsa.num_states + 1
            # add appropriate number of states
            if lemma_idx == 0:
              # set source and target node for first phoneme variation
              source_node = self.fsa.num_states
              target_node = self.fsa.num_states + 1
              self.fsa.num_states += 1
            else:
              if phon_idx != 0:
                self.fsa.num_states += 1
              # set source and target node for split / merge
              source_node = split_node if phon_idx == 0 else self.fsa.num_states
              target_node = merge_node if phon_idx == len(lem) - 1 else self.fsa.num_states + 1
          # edge creation
          phon_edge = Edge(source_node, target_node, phon)
          # triphone labels if current pos at first or last phon
          if phon_idx == 0:
            phon_edge.label_prev = ''
          else:
            phon_edge.label_prev = lem[phon_idx - 1]
          if phon_idx == len(lem) - 1:
            phon_edge.label_next = ''
          else:
            phon_edge.label_next = lem[phon_idx + 1]
          # assign score
          if phon_idx == 0:
            phon_edge.score = lemma['score']
          # position of phon in word and word in sentence
          phon_edge.idx_word_in_sentence = word_idx
          phon_edge.idx_phon_in_word = phon_idx
          phon_edge.idx = self.fsa.num_states + phon_idx
          # phon at word begin / end
          if phon_idx == 0:
            phon_edge.phon_at_word_begin = True
          if phon_idx == len(lem) - 1:
            phon_edge.phon_at_word_end = True
          # add to graph
          self.fsa.edges.append(phon_edge)
      # add silence and eps after word
      self.fsa.edges.append(Edge(target_node, self.fsa.num_states + 1, Edge.SIL))
      self.fsa.edges.append(Edge(target_node, self.fsa.num_states + 1, Edge.EPS))
      self.fsa.num_states += 1
    # final node
    self.fsa.num_states += 1

    edges_allo_tmp = []
    if self.allo_num_states > 1:
      for edge in self.fsa.edges:  # do not add to list you are looping over XD
        if edge.label != Edge.SIL and edge.label != Edge.EPS:
          allo_target_idx = edge.target_state_idx
          for state in range(self.allo_num_states):
            if state == 0:
              edge.target_state_idx = self.fsa.num_states
              edge.allo_idx = state
            elif state == self.allo_num_states - 1:
              edge_1 = deepcopy(edge)
              edge_1.allo_idx = state
              edge_1.source_state_idx = self.fsa.num_states
              edge_1.target_state_idx = allo_target_idx
              self.fsa.num_states += 1
              edges_allo_tmp.append(edge_1)
            else:
              self.fsa.num_states += 1
              edge_2 = deepcopy(edge)
              edge_2.allo_idx = state
              edge_2.source_state_idx = self.fsa.num_states - 1
              edge_2.target_state_idx = self.fsa.num_states
              edges_allo_tmp.append(edge_2)
      self.fsa.edges.extend(edges_allo_tmp)

    sort_idx = 0
    while sort_idx < len(self.fsa.edges):
      cur_source_state = self.fsa.edges[sort_idx].source_state_idx
      cur_target_state = self.fsa.edges[sort_idx].target_state_idx

      if cur_source_state > cur_target_state:  # swap is needed
        edges_with_cur_source_state = self._find_node_in_edges(
          cur_source_state, self.fsa.edges)  # find start node in all edges
        edges_with_cur_target_state = self._find_node_in_edges(
          cur_target_state, self.fsa.edges)  # find end node in all edges

        for edge_key in edges_with_cur_source_state.keys():  # loop over edges with specific node
          if edges_with_cur_source_state[edge_key] == 0:  # swap source state
            self.fsa.edges[edge_key].source_state_idx = cur_target_state
          elif edges_with_cur_source_state[edge_key] == 1:
            self.fsa.edges[edge_key].target_state_idx = cur_target_state
          elif edges_with_cur_source_state[edge_key] == 2:
            self.fsa.edges[edge_key].source_state_idx = cur_target_state
            self.fsa.edges[edge_key].target_state_idx = cur_target_state
          else:
            assert False, ("Dict has a non matching value:",
                           edge_key, edges_with_cur_source_state[edge_key])

        for edge_key in edges_with_cur_target_state.keys():  # edge_key: idx from edge in edges
          if edges_with_cur_target_state[edge_key] == 0:  # swap target state
            self.fsa.edges[edge_key].source_state_idx = cur_source_state
          elif edges_with_cur_target_state[edge_key] == 1:
            self.fsa.edges[edge_key].target_state_idx = cur_source_state
          elif edges_with_cur_target_state[edge_key] == 2:
            self.fsa.edges[edge_key].source_state_idx = cur_source_state
            self.fsa.edges[edge_key].target_state_idx = cur_source_state
          else:
            assert False, ("Dict has a non matching value:",
                           edge_key, edges_with_cur_source_state[edge_key])

        # reset idx: restarts traversing at the beginning of graph
        # swapping may introduce new disorders
        sort_idx = 0

      sort_idx += 1

    # add loops
    for state in range(1, self.fsa.num_states):
      edges_included = [edge_index for edge_index, edge in enumerate(self.fsa.edges) if
                        (edge.target_state_idx == state and edge.label != Edge.EPS)]
      for edge_inc in edges_included:
        edge_loop = deepcopy(self.fsa.edges[edge_inc])
        edge_loop.source_state_idx = edge_loop.target_state_idx
        self.fsa.edges.append(edge_loop)

    # state tying labels or numbers
    for edge in self.fsa.edges:
      allo_syntax = self._build_allo_syntax_for_mapping(edge)
      edge.label = allo_syntax

      if self.state_tying_conversion:
        if edge.label == Edge.EPS:
          pass
        elif allo_syntax in self.state_tying.allo_map:
          allo_id = self.state_tying.allo_map[allo_syntax]
          edge.label = allo_id
        else:
          print("Error converting label:", edge.label, allo_syntax)

    self.fsa.edges.sort()
    self.fsa.num_states_hmm = deepcopy(self.fsa.num_states)
    self.fsa.num_states = -1
    self.fsa.edges_hmm = deepcopy(self.fsa.edges)
    self.fsa.edges = []


class AllPossibleWordsFsa:
  """
  constructs a fsa from all words in a lexicon
  """

  def __init__(self, fsa):
    """
    takes a lexicon file, laods and conttructs a fsa over all possible words
    :param Graph fsa: the graph which holds the constructed fsa
    """
    self.fsa = fsa
    self.lexicon = None

  def run(self):
    print("Starting All Possible Words FSA Creation")
    for key, value in self.lexicon.lemmas.iteritems():  # for python 3: .items()
      edge = Edge(0, 0, key, 0)
      self.fsa.edges_word.append(edge)
    self.fsa.num_states_word = 1


def load_lexicon(lexicon_name='recog.150k.final.lex.gz', pickleflag=False):
  """
  loads Lexicon
  takes a file, loads the xml and returns as Lexicon
  a pickled file can be loaded for a speed improvement
  where:
    lex.lemmas and lex.phonemes important
  :param str lexicon_name: holds the path and name of the lexicon file
  :param bool pickleflag: flag to indicate if the lexicon datastructure is to be pickled
  :return lexicon: lexicon datastructure
  :rtype: Lexicon
  """
  log.initialize(verbosity=[5])
  lexicon_dumpname = lexicon_name.rstrip('\.gz') + '.pickle'

  if pickleflag:
    # loads from pickled lexicon file
    if isfile(lexicon_dumpname):
      print("Loading pickled lexicon")
      with open(lexicon_dumpname, 'rb') as lexicon_load:
        lexicon = pickle.load(lexicon_load)
    else:  # pickled lexicon file does not exists -> now created
      assert isfile(lexicon_name), "Lexicon file does not exist"
      lexicon = Lexicon(lexicon_name)
      print("Saving pickled lexicon")
      with open(lexicon_dumpname, 'wb') as lexicon_dump:
        pickle.dump(lexicon, lexicon_dump)
  else:
    # loads from non-pickled lexicon file
    assert isfile(lexicon_name), "Lexicon file does not exist"
    lexicon = Lexicon(lexicon_name)

  return lexicon


def load_state_tying(state_tying_name='state-tying.txt'):
  """
  loads a state tying map from a file, loads the file and returns its content
  state tying slower with pickling
  where:
    statetying.allo_map important
  :param state_tying_name: holds the path and name of the state tying file
  :return state_tying: state tying datastructure
  :rtype: StateTying
  """
  log.initialize(verbosity=[5])
  assert isfile(state_tying_name), "State tying file does not exist"
  state_tying = StateTying(state_tying_name)

  return state_tying


class Store:
  """
  Conversion and save class for FSA
  """

  def __init__(self, num_states, edges, filename='edges', path='./tmp/', file_format='svg'):
    """
    :param int num_states: number of states of FSA
    :param list[Edge] edges: list of edges representing FSA
    :param str filename: name of the output file
    :param str path: location
    :param str file_format: format in which to save the file
    """
    self.num_states = num_states
    self.edges = edges
    self.filename = filename
    self.path = path
    self.file_format = file_format

    # noinspection PyPackageRequirements,PyUnresolvedReferences
    import graphviz
    self.graph = graphviz.Digraph(format=self.file_format)

  def fsa_to_dot_format(self):
    """
    converts num_states and edges within the graph to dot format
    """
    self.add_nodes(self.graph, self.num_states)
    self.add_edges(self.graph, self.edges)

  def save_to_file(self):
    """
    saves dot graph to file
    settings: filename, path
    caution: overwrites already present files
    """
    # noinspection PyArgumentList
    save_path = self.graph.render(filename=self.filename, directory=self.path)
    print("FSA saved in:", save_path)

  @staticmethod
  def label_conversion(edges):
    """
    coverts the string labels to int labels
    :param list[Edge] edges: list of edges describing the fsa graph
    :return edges:
    :rtype: list[Edges]
    """
    for edge in edges:
      lbl = edge.label
      if lbl == Edge.BLANK:
        edge.label = ord(' ')
      elif lbl == Edge.SIL or lbl == Edge.EPS or isinstance(lbl, int):
        pass
      elif isinstance(lbl, str):
        edge.label = ord(lbl)
      else:
        assert False, "Label Conversion failed!"

  @staticmethod
  def add_nodes(graph, num_states):
    """
    add nodes to the dot graph
    :param Digraph graph: add nodes to this graph
    :param int num_states: number of states equal number of nodes
    """
    nodes = []
    for i in range(0, num_states):
      nodes.append(str(i))

    for n in nodes:
        graph.node(n)

  @staticmethod
  def add_edges(graph, edges):
    """
    add edges to the dot graph
    :param Digraph graph: add edges to this graph
    :param list[Edge] edges: list of edges
    """
    for edge in edges:
      if isinstance(edge.label, int):
        label = edge.label
      elif '{' in edge.label:
        label = edge.label
      elif edge.label_prev is not None and edge.label_next is not None:
        label = [edge.label_prev, edge.label, edge.label_next]
        if edge.allo_idx is not None:
          label.append(edge.allo_idx)
      else:
        label = edge.label
      e = ((str(edge.source_state_idx), str(edge.target_state_idx)), {'label': str(label)})
      graph.edge(*e[0], **e[1])


class BuildSimpleFsaOp(theano.Op):
  itypes = (T.imatrix,)
  # the first and last output are actually uint32
  otypes = (T.fmatrix, T.fvector, T.fmatrix)

  def __init__(self, state_models=None):
    if state_models is None:
        state_models = {}

    self.state_models = state_models

  def perform(self, node, inputs, output_storage, params=None):
    labels = inputs[0]

    from_states      = []
    to_states        = []
    emission_idxs    = []
    seq_idxs         = []
    weights          = []
    start_end_states = []

    cur_state = 0
    edges            = []
    weights          = []
    start_end_states = []
    for b in range(labels.shape[1]):
      seq_start_state = cur_state
      for l in range(labels.shape[0]):
        label = labels[l, b]
        if label < 0:
          continue
        state_model = self.state_models.get(labels[l, b], ('default', 0, 0.0))
        params = state_model[1:]
        state_model = state_model[0]
        if state_model == 'default':
          # default state model where we transition to the next label
          length_model, edge_weight = params
          edges.append((cur_state, cur_state + 1, label, length_model, b))
          weights.append(edge_weight)
          cur_state += 1
        elif state_model == 'loop':
          # allow looping in the current state before proceeding to the next one
          length_model, fwd_score, loop_score = params
          edges.append((cur_state, cur_state,     label, length_model, b))
          weights.append(loop_score)
          edges.append((cur_state, cur_state + 1, label, length_model, b))
          weights.append(fwd_score)
          cur_state += 1
        elif state_model == 'double':
          # choose between emitting the label once or twice
          lm_once, lm_twice_1, lm_twice_2, once_score, twice_score = params
          edges.append((cur_state,     cur_state + 2, label, lm_once, b))
          weights.append(once_score)
          edges.append((cur_state    , cur_state + 1, label, lm_twice_1, b))
          weights.append(0.5 * twice_score)
          edges.append((cur_state + 1, cur_state + 2, label, lm_twice_2, b))
          weights.append(0.5 * twice_score)
          cur_state += 2

      start_end_states.append([seq_start_state, cur_state])

      cur_state += 1

    edges = sorted(edges, key=lambda e: e[1] - e[0])

    output_storage[0][0] = numpy.asarray(edges, dtype='uint32').T.copy().view(dtype='float32')
    output_storage[1][0] = numpy.array(weights, dtype='float32')
    output_storage[2][0] = numpy.asarray(start_end_states, dtype='uint32').T.copy().view(dtype='float32')


class FastBaumWelchBatchFsa:
  """
  FSA(s) in representation format for :class:`FastBaumWelchOp`.
  """

  def __init__(self, edges, weights, start_end_states):
    """
    :param numpy.ndarray edges: (4,num_edges), edges of the graph (from,to,emission_idx,sequence_idx)
    :param numpy.ndarray weights: (num_edges,), weights of the edges
    :param numpy.ndarray start_end_states: (2, batch), (start,end) state idx in automaton.
    """
    assert edges.ndim == 2
    self.num_edges = edges.shape[1]
    assert edges.shape == (4, self.num_edges)
    assert weights.shape == (self.num_edges,)
    assert start_end_states.ndim == 2
    self.num_batch = start_end_states.shape[1]
    assert start_end_states.shape == (2, self.num_batch)
    self.edges = edges
    self.weights = weights
    self.start_end_states = start_end_states


class FastBwFsaShared:
  """
  One FSA shared for all the seqs in one batch (i.e. across batch-dim).
  This is a simplistic class which provides the necessary functions to
  """

  def __init__(self):
    self.num_states = 1
    self.edges = []  # type: list[Edge]

  def add_edge(self, source_state_idx, target_state_idx, emission_idx, weight=0.0):
    """
    :param int source_state_idx:
    :param int target_state_idx:
    :param int emission_idx:
    :param float weight:
    """
    edge = Edge(source_state_idx=source_state_idx, target_state_idx=target_state_idx, label=emission_idx, weight=weight)
    self.num_states = max(self.num_states, edge.source_state_idx + 1, edge.target_state_idx + 1)
    self.edges.append(edge)

  def add_inf_loop(self, state_idx, num_emission_labels):
    """
    :param int state_idx:
    :param int num_emission_labels:
    """
    for emission_idx in range(num_emission_labels):
      self.add_edge(source_state_idx=state_idx, target_state_idx=state_idx, emission_idx=emission_idx)

  def get_num_edges(self, n_batch):
    """
    :param int n_batch:
    :rtype: int
    """
    return len(self.edges) * n_batch

  def get_edges(self, n_batch):
    """
    :param int n_batch:
    :return edges: (4,num_edges), edges of the graph (from,to,emission_idx,sequence_idx)
    :rtype: numpy.ndarray
    """
    num_edges = len(self.edges)
    res = numpy.zeros((4, num_edges * n_batch), dtype="int32")
    for batch_idx in range(n_batch):
      for edge_idx, edge in enumerate(self.edges):
        res[:, batch_idx * num_edges + edge_idx] = (
          edge.source_state_idx + batch_idx * self.num_states,
          edge.target_state_idx + batch_idx * self.num_states,
          edge.label,
          batch_idx)
    return res

  def get_weights(self, n_batch):
    """
    :param int n_batch:
    :return weights: (num_edges,), weights of the edges
    :rtype: numpy.ndarray
    """
    num_edges = len(self.edges)
    res = numpy.zeros((num_edges * n_batch,), dtype="float32")
    for batch_idx in range(n_batch):
      for edge_idx, edge in enumerate(self.edges):
        res[batch_idx * num_edges + edge_idx] = edge.weight
    return res

  def get_start_end_states(self, n_batch):
    """
    :param int n_batch:
    :return start_end_states: (2, batch), (start,end) state idx in automaton. there is only one single automaton.
    :rtype: numpy.ndarray
    """
    start_state_idx = 0
    end_state_idx = self.num_states - 1
    res = numpy.zeros((2, n_batch), dtype="int32")
    for batch_idx in range(n_batch):
      res[:, batch_idx] = (
        start_state_idx + batch_idx * self.num_states,
        end_state_idx + batch_idx * self.num_states)
    return res

  def get_fast_bw_fsa(self, n_batch):
    """
    :param int n_batch:
    :rtype: FastBaumWelchBatchFsa
    """
    return FastBaumWelchBatchFsa(
      edges=self.get_edges(n_batch),
      weights=self.get_weights(n_batch),
      start_end_states=self.get_start_end_states(n_batch))


class LoadWfstOp(theano.Op):
  """
  Op: maps segment names (tags) to fsa automata (load from disk) that can be used to compute a BW-alignment
  """

  __props__ = ("filename",)

  def __init__(self, filename):
    super(LoadWfstOp, self).__init__()
    from Util import make_hashable
    self.filename = make_hashable(filename)
    self.single_wfst = None  # type: dict

  def make_node(self, tags):
    # the edges/start_end_state output has to be a float matrix because that is the only dtype supported
    # by CudaNdarray. We need unsigned ints. Thus we return a view on the unsigned int matrix
    return theano.Apply(self, [tags], [T.fmatrix(), T.fvector(), T.fvector(), T.fmatrix(), T.fvector(), T.fmatrix()])

  def perform(self, node, inputs, output_storage, params=None):
    tags = inputs[0]
    try:
      _ = iter(tags)
    except TypeError:
      tags = [tags]

    if self.single_wfst is None:
      print("LoadWfstOp: Loading WFST from %r" % self.filename, file=log.v3)
      import xml.etree.ElementTree as ET

      tree = ET.parse(self.filename)
      root = tree.getroot()
      single_wfst = dict()
      single_wfst['edges'] = []
      single_wfst['weights'] = []
      single_wfst['start_states'] = numpy.array([root.attrib['initial']],dtype=numpy.uint32)
      single_wfst['end_states'] = []
      single_wfst['end_state_weigths'] = []
      self.single_wfst = dict()
      self.single_wfst['num_states'] = len(root)

      for state in root:
        if state.tag != 'state':
          continue # not interested in input-alphabet
        state_id = numpy.uint32(state.attrib['id'])
        if state[0].tag == 'final':
            single_wfst['end_states'].append([numpy.uint32(0),state_id])
            if state[1].tag == 'weight':
              single_wfst['end_state_weigths'].append(numpy.float32(state[1].text))
            else:
              single_wfst['end_state_weigths'].append(numpy.float32(0.))
        for arc in state:
          if arc.tag != 'arc':
            continue # alredy handeled 'final' and 'weight'
          target = numpy.uint32(arc.attrib['target'])
          emission_id = numpy.uint32(arc[0].text)
          if len(arc) > 1 :
            weight = numpy.float32(arc[1].text)
          else:
            weight = numpy.float32(0.)
          single_wfst['edges'].append([state_id,target,emission_id,numpy.uint32(0)])
          single_wfst['weights'].append(weight)
      for key,val in single_wfst.items():
        self.single_wfst[key] = numpy.array(val)

    assert isinstance(self.single_wfst, dict)  # PyCharm confused otherwise

    offset = 0
    all_edges = []
    all_weights = []
    all_start_states = []
    all_end_states = []
    all_end_state_weigths = []
    for tag in tags:
      edges = numpy.transpose(numpy.copy(self.single_wfst['edges']))
      edges[0:2,:] += offset
      edges[3,:]    = tag
      all_edges.append(edges)
      all_weights.append(self.single_wfst['weights'])
      all_start_states.append(self.single_wfst['start_states']+offset)
      end_states = numpy.copy(self.single_wfst['end_states'])
      end_states[:,1] += offset
      end_states[:,0]    = tag
      all_end_states.append(end_states)
      all_end_state_weigths.append(self.single_wfst['end_state_weigths'])
      offset += self.single_wfst['num_states']

    output_storage[0][0] = numpy.hstack(all_edges).view(dtype='float32')
    output_storage[1][0] = numpy.hstack(all_weights)
    output_storage[2][0] = numpy.hstack(all_start_states).view(dtype='float32')
    output_storage[3][0] = numpy.hstack(all_end_states).view(dtype='float32')
    output_storage[4][0] = numpy.hstack(all_end_state_weigths)
    output_storage[5][0] = numpy.empty((2, self.single_wfst['num_states']*len(tags)), dtype='float32')


def main():
  import time
  from argparse import ArgumentParser

  arg_parser = ArgumentParser()
  arg_parser.add_argument("--fsa", type=str)
  arg_parser.add_argument("--label_seq", type=str, required=True)
  arg_parser.add_argument("--file", type=str)
  arg_parser.set_defaults(file='fsa')
  arg_parser.add_argument("--asg_repetition", type=int)
  arg_parser.set_defaults(asg_repetition=3)
  arg_parser.add_argument("--num_labels", type=int)
  arg_parser.set_defaults(num_labels=265)  # ascii number of labels
  arg_parser.add_argument("--label_conversion", dest="label_conversion", action="store_true")
  arg_parser.add_argument("--no_label_conversion", dest="label_conversion", action="store_false")
  arg_parser.set_defaults(label_conversion=False)
  arg_parser.add_argument("--depth", type=int)
  arg_parser.set_defaults(depth=6)
  arg_parser.add_argument("--allo_num_states", type=int)
  arg_parser.set_defaults(allo_num_states=3)
  arg_parser.add_argument("--lexicon", type=str)
  arg_parser.set_defaults(lexicon='recog.150k.final.lex.gz')
  arg_parser.add_argument("--state_tying", type=str)
  arg_parser.set_defaults(state_tying='state-tying.txt')
  arg_parser.add_argument("--state_tying_conversion",
                          dest="state_tying_conversion", action="store_true")
  arg_parser.add_argument("--no_state_tying_conversion",
                          dest="state_tying_conversion", action="store_false")
  arg_parser.set_defaults(state_tying_conversion=False)
  arg_parser.add_argument("--single_state", dest="single_state", action="store_true")
  arg_parser.add_argument("--no_single_state", dest="single_state", action="store_false")
  arg_parser.set_defaults(single_state=False)
  arg_parser.add_argument("--asg_separator", type=bool)
  arg_parser.set_defaults(asg_separator=False)
  arg_parser.add_argument("--pickle", dest="pickle", action="store_true")
  arg_parser.add_argument("--no_pickle", dest="pickle", action="store_false")
  arg_parser.set_defaults(pickle=False)
  args = arg_parser.parse_args()

  start_time = time.time()

  fsa = Graph(lemma=args.label_seq)


  lexicon_start_time = time.time()
  lexicon = load_lexicon(args.lexicon, args.pickle)
  lexicon_end_time = time.time()

  word_start_time = time.time()

  word = AllPossibleWordsFsa(fsa)
  word.lexicon = lexicon
  word_run_start_time = time.time()
  word.run()
  word_run_end_time = time.time()
  sav_word = Store(fsa.num_states_word, fsa.edges_word)
  sav_word.filename = 'edges_word'
  sav_word.fsa_to_dot_format()
  sav_word.save_to_file()

  asg_start_time = word_end_time = time.time()

  asg = Asg(fsa)
  asg.label_conversion = args.label_conversion
  asg.asg_repetition = args.asg_repetition
  asg_run_start_time = time.time()
  asg.run()
  asg_run_end_time = time.time()
  sav_asg = Store(fsa.num_states_asg, fsa.edges_asg)
  sav_asg.filename = 'edges_asg'
  sav_asg.fsa_to_dot_format()
  sav_asg.save_to_file()

  asg_end_time = ctc_start_time = time.time()

  ctc = Ctc(fsa)
  ctc.label_conversion = args.label_conversion
  ctc_run_start_time = time.time()
  ctc.run()
  ctc_run_end_time = time.time()
  sav_ctc = Store(fsa.num_states_ctc, fsa.edges_ctc)
  sav_ctc.filename = 'edges_ctc'
  sav_ctc.fsa_to_dot_format()
  sav_ctc.save_to_file()

  ctc_end_time = hmm_start_time = time.time()

  hmm = Hmm(fsa)
  hmm.lexicon = lexicon
  hmm.state_tying = load_state_tying(args.state_tying)
  hmm.allo_num_states = args.allo_num_states
  hmm.state_tying_conversion = args.state_tying_conversion
  hmm_run_start_time = time.time()
  hmm.run()
  hmm_run_end_time = time.time()
  sav_hmm = Store(fsa.num_states_hmm, fsa.edges_hmm)
  sav_hmm.filename = 'edges_hmm'
  sav_hmm.fsa_to_dot_format()
  sav_hmm.save_to_file()

  end_time = hmm_end_time = time.time()

  print("\nTotal time    : ", end_time - start_time, "\n")

  print("Lexicon load time : ", lexicon_end_time - lexicon_start_time, "\n")

  print("Word total time: ", word_end_time - word_start_time)
  print("Word init time : ", word_run_start_time - word_start_time)
  print("Word run time  : ", word_run_end_time - word_run_start_time)
  print("Word save time : ", word_end_time - word_run_end_time, "\n")

  print("ASG total time: ", asg_end_time - asg_start_time)
  print("ASG init time : ", asg_run_start_time - asg_start_time)
  print("ASG run time  : ", asg_run_end_time - asg_run_start_time)
  print("ASG save time : ", asg_end_time - asg_run_end_time, "\n")

  print("CTC total time: ", ctc_end_time - ctc_start_time)
  print("CTC init time : ", ctc_run_start_time - ctc_start_time)
  print("CTC run time  : ", ctc_run_end_time - ctc_run_start_time)
  print("CTC save time : ", ctc_end_time - ctc_run_end_time, "\n")

  print("HMM total time: ", hmm_end_time - hmm_start_time)
  print("HMM init time : ", hmm_run_start_time - hmm_start_time)
  print("HMM run time  : ", hmm_run_end_time - hmm_run_start_time)
  print("HMM save time : ", hmm_end_time - hmm_run_end_time, "\n")


if __name__ == "__main__":
  main()
