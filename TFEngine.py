"""
TensorFlow engine
=================

The basic engine for the TensorFlow backend is implemented here,
i.e. the high-level logic to train, i.e. looping over epochs,
holding the network instance, creating the TensorFlow session,
managing the data pipeline, etc.

See :ref:`tech_overview` for an overview how it fits all together.
"""

from __future__ import print_function

import os
import sys
import time
try:
  # noinspection PyCompatibility
  from Queue import Queue
except ImportError:
  # noinspection PyCompatibility
  from queue import Queue

import numpy
import tensorflow as tf
from tensorflow.python.client import timeline

from Dataset import Dataset, Batch, BatchSetGenerator
from Engine import Engine as TheanoEngine
from LearningRateControl import loadLearningRateControlFromConfig
from Log import log
from Network import LayerNetwork
from Pretrain import pretrainFromConfig
from TFNetwork import TFNetwork, ExternData
from TFUpdater import Updater
from Util import hms, NumbersDict


class Runner(object):
  def __init__(self, engine, dataset, batches, train, eval=True, extra_fetches=None, extra_fetches_callback=None):
    """
    :param Engine engine:
    :param Dataset.Dataset dataset:
    :param BatchSetGenerator batches:
    :param bool train: whether to do updates on the model
    :param bool eval: whether to evaluate (i.e. calculate loss/error)
    :param dict[str,tf.Tensor|TFUtil.Data|TFNetworkLayer.LayerBase]|None extra_fetches: additional fetches per step.
      `extra_fetches_callback` will be called with these. In case of Data/LayerBase, it will return a list,
      where each item corresponds to the batch-seq.
      It might also be useful to add `network.get_extern_data("seq_idx")` and `network.get_extern_data("seq_tag")`.
    :param (**dict[str,numpy.ndarray|str|list[numpy.ndarray|str])->None extra_fetches_callback: called if extra_fetches
    """
    from TFDataPipeline import FeedDictDataProvider, DataProviderBase
    engine.network.extern_data.check_matched_dataset(
      dataset=dataset, used_data_keys=engine.network.used_data_keys)
    self.engine = engine
    self.data_provider = FeedDictDataProvider(
      tf_session=engine.tf_session, extern_data=engine.network.extern_data,
      data_keys=engine.network.used_data_keys,
      dataset=dataset, batches=batches)
    assert isinstance(self.data_provider, DataProviderBase)
    self._should_train = train
    self._should_eval = eval
    self.store_metadata_mod_step = engine.config.int("store_metadata_mod_step", 0)
    self.reset_updater_vars_mod_step = engine.config.int("reset_updater_vars_mod_step", 0)
    self.finalized = False
    self.num_steps = None
    self.device_crash_batch = None  # type: int|None
    self.start_time = None
    self.elapsed = None
    self._results_accumulated = {}  # type: dict[str,float]  # entries like "cost:output" or "loss"
    self.num_frames_accumulated = NumbersDict()  # for each result key, the corresponding number of frames
    self.results = {}  # type: dict[str,float]  # entries like "cost:output" or "loss"
    self.score = {}  # type: dict[str,float]  # entries like "cost:output"
    self.error = {}  # type: dict[str,float]  # entries like "error:output"
    self.stats = {}  # type: dict[str,float]  # entries like "stats:..."
    self.extra_fetches = extra_fetches
    if extra_fetches is not None:
      assert extra_fetches_callback
    self.extra_fetches_callback = extra_fetches_callback

    from Util import terminal_size
    terminal_width, _ = terminal_size()
    self._show_interactive_process_bar = (log.verbose[3] and (not log.verbose[5]) and terminal_width >= 0)

  def _get_fetches_dict(self):
    """
    :return: values and actions which should be calculated and executed in self.run() by the TF session for each step
    :rtype: dict[str,tf.Tensor|tf.Operation]
    """
    # Note that it is important that we do not recreate graph nodes for every call to this function.
    # Thus everything which we access here should be cached.
    d = {}
    for key in self.data_provider.data_keys:
      data = self.data_provider.extern_data.get_data(key)
      for dim, v in data.size_placeholder.items():
        d["size:%s:%i" % (key, dim)] = v
    if self._should_train or self._should_eval:
      # These values are cached internally and the graph nodes are created on the first call.
      loss = self.engine.network.get_objective()
      if loss is 0:
        loss = self.engine.get_const_tensor(key="zero_loss", value=0.0)
      d["loss"] = loss
      for layer_name, loss in self.engine.network.loss_by_layer.items():
        if self.engine.network.layers[layer_name].only_on_eval and self._should_train:
          continue
        d["cost:%s" % layer_name] = loss
      for layer_name, error in self.engine.network.error_by_layer.items():
        if self.engine.network.layers[layer_name].only_on_eval and self._should_train:
          continue
        d["error:%s" % layer_name] = error
      for layer in self.engine.network.layers.values():
        if layer.target and layer.target.startswith("layer:"):
          target_data = layer.loss.target
          for dim, v in target_data.size_placeholder.items():
            d["size:%s:%i" % (layer.target, dim)] = v
    for layer in self.engine.network.layers.values():
      for k, v in layer.stats.items():
        d["stats:%s:%s" % (layer.name, k)] = v
    if self._should_train:
      assert self.engine.updater
      def callback_on_new():
        # Force a new check.
        self.engine._checked_uninitialized_vars = False
      d["optim_op"] = self.engine.updater.get_optim_op(callback_on_new=callback_on_new)
      if self.engine.updater.optim_meta_losses:
        d.update(self.engine.updater.optim_meta_losses)
    if self.extra_fetches is not None:
      from TFNetworkLayer import LayerBase
      from TFUtil import Data
      for k, v in self.extra_fetches.items():
        if isinstance(v, tf.Tensor):
          d["extra:%s" % k] = v
          continue
        if isinstance(v, LayerBase):
          v = v.output
        assert isinstance(v, Data)
        d["extra:%s" % k] = v.placeholder
        for i, s in v.size_placeholder.items():
          d["extra:%s:size_%i" % (k, i)] = s
    if self.engine.get_all_merged_summaries() is not None:
      d["summary"] = self.engine.get_all_merged_summaries()
    return d

  def _print_process(self, report_prefix, step, step_duration, eval_info):
    if not self._show_interactive_process_bar and not log.v[5]:
      return
    start_elapsed = time.time() - self.start_time
    complete = self.data_provider.get_complete_frac()
    assert complete > 0
    total_time_estimated = start_elapsed / complete
    remaining_estimated = total_time_estimated - start_elapsed
    if log.verbose[5]:
      info = [
        report_prefix,
        "step %i" % step]
      if eval_info:  # Such as score.
        info += ["%s %s" % item for item in sorted(eval_info.items())]
      info += [
        "%.3f sec/step" % step_duration,
        "elapsed %s" % hms(start_elapsed),
        "exp. remaining %s" % hms(remaining_estimated),
        "complete %.02f%%" % (complete * 100)]
      print(", ".join(filter(None, info)), file=log.v5)
    elif self._show_interactive_process_bar:
      from Util import progress_bar
      progress_bar(complete, hms(remaining_estimated))

  def _print_finish_process(self):
    if self._show_interactive_process_bar:
      from Util import progress_bar
      progress_bar()

  def _get_target_for_key(self, key):
    """
    :param str key: e.g. "cost:output" where the last part is the layer name. or "loss"
    :return: target name which is the data-key in the dataset, e.g. "classes"
    :rtype: str
    """
    if ":" in key:
      layer = self.engine.network.layers[key.split(':')[-1]]
      if layer.target:
        return layer.target
    return self.engine.network.extern_data.default_target

  def _epoch_norm_factor_for_result(self, key):
    """
    :param str key: e.g. "cost:output"
    :return: factor to multiply with such accumulated values for the final epoch stats
    :rtype: float
    """
    # Default: Normalize by number of frames.
    return 1.0 / self.num_frames_accumulated[key]

  def _finalize(self, num_steps):
    """
    Called at the end of an epoch.
    :param int num_steps: number of steps we did for this epoch
    """
    assert not self.data_provider.have_more_data(session=self.engine.tf_session)
    results = {key: value * self._epoch_norm_factor_for_result(key)
               for (key, value) in self._results_accumulated.items()}
    self.results = results
    self.score = {key: value for (key, value) in results.items() if key.startswith("cost:")}
    self.error = {key: value for (key, value) in results.items() if key.startswith("error:")}
    self.num_steps = num_steps
    self.finalized = True

  def _step_seq_len(self, fetches_results, data_key):
    """
    :param dict[str,numpy.ndarray|None] fetches_results: results of calculations, see self._get_fetches_dict()
    :param str data_key: e.g. "classes"
    :return: the seq length of this batch
    :rtype: int
    """
    num_frames = numpy.sum(fetches_results["size:%s:0" % data_key])
    return num_frames

  def _collect_eval_info(self, fetches_results):
    """
    :param dict[str,numpy.ndarray|None] fetches_results: results of calculations, see self._get_fetches_dict()
    :return: dict for printing the step stats, see self._print_process(), e.g. {"cost:output": 2.3}
    :rtype: dict[str,float]
    """
    # See see self._get_fetches_dict() for the keys.
    keys = [k for k in fetches_results.keys() if k.startswith("cost:") or k.startswith("error:") or k == "loss"]
    step_seq_lens = {}  # key -> int
    for key in keys:
      target = self._get_target_for_key(key)
      step_seq_lens[key] = self._step_seq_len(
        fetches_results=fetches_results, data_key=target)

    # Accumulate for epoch stats.
    self.num_frames_accumulated += NumbersDict(step_seq_lens)
    for key in keys:
      value = fetches_results[key]
      if key not in self._results_accumulated:
        self._results_accumulated[key] = value
      else:
        self._results_accumulated[key] += value

    # Prepare eval info stats for this batch run.
    eval_info = {}
    for key in keys:
      value = fetches_results[key]
      if value:
        value /= float(step_seq_lens[key])
      eval_info[key] = value

    # Add raw stats.
    for k, v in fetches_results.items():
      if k.startswith("stats:"):
        if v.ndim == 1:
          v = list(v)  # looks nicer in logs
        eval_info[k] = v
        self.stats[k] = v  # Always just store latest value.

    return eval_info

  def _maybe_handle_extra_fetches(self, fetches_results):
    """
    :param dict[str,numpy.ndarray|str] fetches_results: results of calculations, see self._get_fetches_dict()
    """
    if self.extra_fetches is None:
      return
    d = {}
    from TFNetworkLayer import LayerBase
    from TFUtil import Data
    for k, v in self.extra_fetches.items():
      r = fetches_results["extra:%s" % k]
      if isinstance(v, tf.Tensor):
        d[k] = r
        continue
      if isinstance(v, LayerBase):
        v = v.output
      assert isinstance(v, Data)
      if v.batch_dim_axis != 0:
        r = numpy.moveaxis(r, v.batch_dim_axis, 0)
      if v.have_time_axis():
        assert v.time_dim_axis_excluding_batch == 0
        assert list(v.size_placeholder.keys()) == [0]
        seq_lens = fetches_results["extra:%s:size_0" % k]  # shape: (batch,)
        assert seq_lens.shape == (r.shape[0],)
        d[k] = [r[i, :seq_lens[i]] for i in range(seq_lens.shape[0])]
      else:
        d[k] = list(r)
    self.extra_fetches_callback(**d)

  def run(self, report_prefix):
    """
    :param str report_prefix: prefix for logging
    """
    sess = self.engine.tf_session
    if self.engine.config.has("tf_log_dir"):
      logdir = self.engine.config.value("tf_log_dir", None)
    else:
      logdir = os.path.dirname(self.engine.model_filename) or os.getcwd()
    if logdir:
      from Util import log_runtime_info_to_dir
      log_runtime_info_to_dir(logdir, config=self.engine.config)
      logdir += "/%s" % self.data_provider.get_dataset_name()
      if not self._should_train:  # like eval
        logdir += "-%i" % self.engine.epoch
      if self.engine.use_search_flag:
        logdir += "-search"
      writer = tf.summary.FileWriter(logdir)
    else:
      writer = None
    print("TF: log_dir: %s" % logdir, file=log.v5)
    run_metadata = tf.RunMetadata()
    debug_shell_in_runner = self.engine.config.bool("debug_shell_in_runner", False)
    debug_shell_in_runner_step = self.engine.config.int("debug_shell_in_runner_step", 1)

    # Not sure if this is the best thing to do for an evaluation but it's ok for now.
    # We could also set it to 0 for non train epochs.
    step_offset = self.engine.network.get_global_train_step(session=sess)

    coord = self.data_provider.coord

    threads = tf.train.start_queue_runners(sess=sess, coord=coord)
    self.data_provider.start_threads()
    self.start_time = time.time()
    step = None
    try:
      # step is like mini-batch in our usual terminology
      step = 0
      fetches_dict = self._get_fetches_dict()
      # After get_fetches_dict, maybe some new uninitialized vars. Last check.
      self.engine.check_uninitialized_vars()
      # Also, add graph to summary here because the updater/optimizer might not have been created before.
      if writer:
        writer.add_graph(sess.graph)
      while self.data_provider.have_more_data(session=sess):
        feed_dict = self.data_provider.get_feed_dict()
        if isinstance(self.engine.network.train_flag, tf.Tensor):
          feed_dict[self.engine.network.train_flag] = self._should_train
        start_time = time.time()
        if self._should_train and self.reset_updater_vars_mod_step and step % self.reset_updater_vars_mod_step == 0:
          print("Reset updater vars in step %i." % step, file=log.v5)
          self.engine.updater.init_optimizer_vars()

        if debug_shell_in_runner and debug_shell_in_runner_step == step:
          print("debug_shell_in_runner, step %i" % step, file=log.v1)
          import Debug
          Debug.debug_shell(user_ns=locals(), user_global_ns=globals(), exit_afterwards=False)

        # Now do one calculation step. Optionally with metadata.
        if self.store_metadata_mod_step and step % self.store_metadata_mod_step == 0:
          # Slow run that stores extra information for debugging.
          print('Storing metadata', file=log.v5)
          run_options = tf.RunOptions(
            trace_level=tf.RunOptions.FULL_TRACE)
          # We could use tfdbg.add_debug_tensor_watch here.
          fetches_results = sess.run(
            fetches_dict,
            feed_dict=feed_dict,
            options=run_options,
            run_metadata=run_metadata)  # type: dict[str,numpy.ndarray|str]
          writer.add_summary(fetches_results["summary"], step + step_offset)
          writer.add_run_metadata(run_metadata, 'step_{:04d}'.format(step + step_offset))
          tl = timeline.Timeline(run_metadata.step_stats)
          timeline_path = os.path.join(logdir, 'timeline.trace')
          with open(timeline_path, 'w') as f:
            f.write(tl.generate_chrome_trace_format(show_memory=True))
        else:
          fetches_results = sess.run(fetches_dict, feed_dict=feed_dict)  # type: dict[str,numpy.ndarray|str]
          if writer and "summary" in fetches_results:
            writer.add_summary(fetches_results["summary"], step + step_offset)

        eval_info = self._collect_eval_info(fetches_results=fetches_results)
        self._maybe_handle_extra_fetches(fetches_results)
        duration = time.time() - start_time
        self._print_process(report_prefix=report_prefix, step=step, step_duration=duration, eval_info=eval_info)
        step += 1

      self._print_finish_process()

      if not self.data_provider.have_reached_end():
        raise Exception("Did not successfully reached the end of the dataset.")

      if self._should_train:
        final_global_train_step = self.engine.network.get_global_train_step(session=sess)
        assert step + step_offset == final_global_train_step

      self._finalize(num_steps=step)

      if self.engine.config.bool("tf_log_memory_usage", False):
        print("Memory usage:", file=log.v1)
        from TFUtil import get_tf_list_local_devices, mem_usage_for_dev
        from Util import human_bytes_size
        for dev in get_tf_list_local_devices():
          if dev.device_type != "GPU":
            # mem_usage_for_dev currently only works for GPU
            continue
          size = sess.run(mem_usage_for_dev(dev.name))
          print(" %s: %s" % (dev.name, human_bytes_size(size)), file=log.v1)

    except KeyboardInterrupt:
      print("KeyboardInterrupt in step %r." % step)

    except BaseException as exc:
      print("Exception %r in step %r." % (exc, step), file=log.v1)
      sys.excepthook(*sys.exc_info())
      self.device_crash_batch = step

    finally:
      from Util import try_and_ignore_exception
      from TFUtil import stop_event_writer_thread
      if writer:
        try_and_ignore_exception(writer.close)
        try_and_ignore_exception(lambda: stop_event_writer_thread(writer.event_writer))
      try_and_ignore_exception(coord.request_stop)
      try_and_ignore_exception(lambda: coord.join(threads))
      try_and_ignore_exception(self.data_provider.stop_threads)
      self.elapsed = time.time() - self.start_time


class Engine(object):
  def __init__(self, config=None):
    """
    :param Config.Config|None config:
    """
    if config is None:
      from Config import get_global_config
      config = get_global_config()
    self.config = config
    self.devices_config = self._get_devices_config()
    self._check_devices()
    self.tf_session = None  # type: tf.Session
    self.network = None  # type: TFNetwork
    self.updater = None  # type: Updater
    self._checked_uninitialized_vars = False
    self._merge_all_summaries = None
    self.dataset_batches = {}  # type: dict[str,BatchSetGenerator]
    self.train_data = None  # type: Dataset
    self.start_epoch = None
    self.use_dynamic_train_flag = False
    self.use_search_flag = config.value("task", None) == "search"
    self.use_eval_flag = config.value("task", None) != "forward"
    self._const_cache = {}  # type: dict[str,tf.Tensor]

  def finalize(self):
    self._close_tf_session()
    tf.reset_default_graph()
    self.network = None
    self.updater = None
    self._merge_all_summaries = None

  def get_const_tensor(self, key, value):
    if key not in self._const_cache:
      self._const_cache[key] = tf.constant(value=value, name="const_%s" % key)
    return self._const_cache[key]

  def _get_devices_config(self):
    """
    :rtype: list[dict[str]]
    """
    from Device import getDevicesInitArgs
    if not self.config.value("device", None):
      # Better default: Use GPU if available.
      from TFUtil import is_gpu_available
      if is_gpu_available():
        print("Device not set explicitly, and we found a GPU, which we will use.", file=log.v2)
        self.config.set("device", "gpu")
      else:
        print("Device not set explicitly, and no GPU found.", file=log.v2)
    return getDevicesInitArgs(self.config)

  def is_requesting_for_gpu(self):
    return any([d["device"].startswith("gpu") for d in self.devices_config])

  def _check_devices(self):
    from TFUtil import print_available_devices, is_gpu_available
    print_available_devices()
    assert len(self.devices_config) == 1, "multiple devices not supported yet for TF"
    if self.is_requesting_for_gpu():
      assert is_gpu_available(), "no GPU available"
    else:
      if is_gpu_available():
        print("Note: There is a GPU available but you have set device=cpu.", file=log.v2)

  def _close_tf_session(self):
    if self.tf_session:
      self.tf_session.close()
    self.tf_session = None

  def _make_tf_session(self):
    self._close_tf_session()
    opts = self.config.typed_value("tf_session_opts", {})
    assert isinstance(opts, dict)
    opts = opts.copy()
    # See options here:
    # https://github.com/tensorflow/tensorflow/blob/master/tensorflow/core/protobuf/config.proto
    opts.setdefault("log_device_placement", False)
    opts.setdefault("device_count", {}).setdefault("GPU", 1 if self.is_requesting_for_gpu() else 0)
    # Note: We don't set intra_op_parallelism_threads and inter_op_parallelism_threads here anymore
    # because it is saver to do it via setup_tf_thread_pools() which we call very early.
    print("Setup tf.Session with options %r ..." % opts, file=log.v2)
    config = tf.ConfigProto(**opts)
    # config.gpu_options.allow_growth=True
    # For debugging, see tfdbg.LocalCLIDebugWrapperSession.
    self.tf_session = tf.Session(config=config)

  def _reset_graph(self):
    tf.reset_default_graph()
    self._checked_uninitialized_vars = False
    self._merge_all_summaries = None
    self._const_cache.clear()

  get_train_start_epoch_batch = TheanoEngine.get_train_start_epoch_batch
  config_get_final_epoch = TheanoEngine.config_get_final_epoch
  get_epoch_model = TheanoEngine.get_epoch_model
  epoch_model_filename = TheanoEngine.epoch_model_filename

  def get_epoch_model_filename(self, epoch=None):
    if not epoch:
      epoch = self.epoch
    return self.epoch_model_filename(self.model_filename, epoch, self.is_pretrain_epoch(epoch=epoch))

  def get_epoch_str(self):
    return ("pretrain " if self.is_pretrain_epoch() else "") + "epoch %s" % self.epoch

  def is_pretrain_epoch(self, epoch=None):
    if not epoch:
      epoch = self.epoch
    return self.pretrain and epoch <= self.pretrain.get_train_num_epochs()

  def is_first_epoch_after_pretrain(self):
    return self.pretrain and self.epoch == self.pretrain.get_train_num_epochs() + 1

  def get_eval_datasets(self):
    eval_datasets = {}; """ :type: dict[str,Dataset.Dataset] """
    for name, dataset in [("dev", self.dev_data), ("eval", self.eval_data)]:
      if not dataset: continue
      eval_datasets[name] = dataset
    return eval_datasets

  def load_model(self, epoch=None, filename=None):
    """
    :param int epoch:
    :param str filename:
    """
    assert epoch or filename
    if epoch:
      assert not filename
      filename = self.get_epoch_model_filename(epoch=epoch)
    print("Load model %s" % (filename,), file=log.v4)
    self.network.load_params_from_file(filename, session=self.tf_session)

  def save_model(self, filename=None):
    """
    :param str filename: full filename for model
    """
    if not filename:
      filename = self.get_epoch_model_filename()
    print("Save model under %s" % (filename,), file=log.v4)
    self.network.save_params_to_file(filename, session=self.tf_session)

  def init_train_from_config(self, config=None, train_data=None, dev_data=None, eval_data=None):
    """
    :param Config.Config|None config:
    :param Dataset.Dataset|None train_data:
    :param Dataset.Dataset|None dev_data:
    :param Dataset.Dataset|None eval_data:
    """
    if not config:
      config = self.config
    self.use_dynamic_train_flag = True
    self.train_data = train_data
    self.dev_data = dev_data
    self.eval_data = eval_data
    self.start_epoch, self.start_batch = self.get_train_start_epoch_batch(config)
    self.batch_size = config.int('batch_size', 1)
    self.shuffle_batches = config.bool('shuffle_batches', True)
    self.update_batch_size = config.int('update_batch_size', 0)
    self.save_model_epoch_interval = config.int('save_interval', 1)
    self.save_epoch1_initial_model = config.bool('save_epoch1_initial_model', False)
    self.learning_rate_control = loadLearningRateControlFromConfig(config)
    self.learning_rate = self.learning_rate_control.defaultLearningRate
    self.initial_learning_rate = self.learning_rate
    self.pretrain_learning_rate = config.float('pretrain_learning_rate', self.learning_rate)
    self.final_epoch = self.config_get_final_epoch(config)  # Inclusive.
    self.max_seqs = config.int('max_seqs', -1)
    self.ctc_prior_file = config.value('ctc_prior_file', None)
    self.exclude = config.int_list('exclude', [])
    self.init_train_epoch_posthook = config.value('init_train_epoch_posthook', None)
    self.share_batches = config.bool('share_batches', False)
    self.seq_drop = config.float('seq_drop', 0.0)
    self.seq_drop_freq = config.float('seq_drop_freq', 10)
    self.max_seq_length = config.float('max_seq_length', 0)
    self.inc_seq_length = config.float('inc_seq_length', 0)
    if self.max_seq_length == 0:
      self.max_seq_length = sys.maxsize
    # And also initialize the network. That depends on some vars here such as pretrain.
    self.init_network_from_config(config)

  def init_network_from_config(self, config):
    """
    :param Config.Config config:
    """
    self.model_filename = config.value('model', None)
    self.pretrain = pretrainFromConfig(config)
    self.max_seqs = config.int('max_seqs', -1)

    epoch, model_epoch_filename = self.get_epoch_model(config)
    if not model_epoch_filename and not self.start_epoch:
      if self.config.bool("allow_random_model_init", False):
        print("No model will be loaded. Randomly initializing model.", file=log.v2)
        epoch = 1
      else:
        raise Exception(
          "You are not using training, otherwise start_epoch would be set via self.init_train_from_config(). "
          "There was also no model found which we could load. Set one via 'load'.")
    self.epoch = epoch or self.start_epoch
    assert self.epoch

    if self.pretrain:
      # This would be obsolete if we don't want to load an existing model.
      # In self.init_train_epoch(), we initialize a new model.
      net_dict = self.pretrain.get_network_json_for_epoch(self.epoch)
    else:
      net_dict = LayerNetwork.json_from_config(config)

    self._init_network(net_desc=net_dict, epoch=self.epoch)

    if model_epoch_filename:
      print("loading weights from", model_epoch_filename, file=log.v2)
      try:
        self.network.load_params_from_file(model_epoch_filename, session=self.tf_session)
      except tf.errors.NotFoundError:
        print("Exiting now because model cannot be loaded.", file=log.v1)
        sys.exit(1)

  def _init_network(self, net_desc, epoch=None):
    if epoch is None:
      epoch = self.epoch
    self._close_tf_session()
    self._reset_graph()
    # The new session will by default use the newly created default graph.
    self._make_tf_session()
    tf.set_random_seed(42)
    from TFUtil import get_global_train_flag_placeholder
    if self.use_dynamic_train_flag:
      train_flag = get_global_train_flag_placeholder()
    else:
      train_flag = False
    if False:  # TODO ...
      extern_data = ExternData()
      extern_data.init_from_config(self.config)
      # TODO...
    network = TFNetwork(
      name="root",
      config=self.config,
      rnd_seed=epoch,
      train_flag=train_flag,
      eval_flag=self.use_eval_flag,
      search_flag=self.use_search_flag)
    network.construct_from_dict(net_desc)
    network.initialize_params(session=self.tf_session)
    network.layers_desc = net_desc
    self.network = network
    if self.train_data:
      # Need to create new Updater because it has the learning_rate var which must be in the current graph.
      self.updater = Updater(config=self.config, tf_session=self.tf_session, network=network)
      self.updater.set_trainable_vars(network.get_trainable_params())
    network.print_network_info()

  def maybe_init_new_network(self, net_desc):
    if self.network.layers_desc == net_desc:
      return
    from Util import dict_diff_str
    print("reinit because network description differs. Diff:",
          dict_diff_str(self.network.layers_desc, net_desc), file=log.v3)
    old_network_params = self.network.get_params_serialized(self.tf_session)
    self._init_network(net_desc)
    # In pretraining it can happen, that the dimension of output parameters of the previous epoch is
    # not equal to the dimension in the current epoch, due to difference in layer size.
    # In that case initialize output parameters randomly
    if self.is_pretrain_epoch():
      # iterate through all output layers and check dimension compatibility of parameters
      # start output layer from random initialization if one parameters dimension do not match
      for l in self.network.get_output_layers():
        keep_layer = True
        if self.pretrain.copy_output_layer is False:
          keep_layer = False
        else:
          if l.name in old_network_params.values_dict:
            for param in l.params:
              if tuple(l.params[param].shape.as_list()) != old_network_params.values_dict[l.name][param].shape:
                keep_layer = False
                break
        if not keep_layer:
          print("suspend copying of output layer: " + l.name, file=log.v2)
          del old_network_params.values_dict[l.name]
    # Otherwise it's initialized randomly which is fine.
    # This copy will copy the old params over and leave the rest randomly initialized.
    # This also works if the old network has just the same topology,
    # e.g. if it is the initial model from self.init_network_from_config().
    self.network.set_params_by_serialized(old_network_params, session=self.tf_session)

  def train(self):
    print("start training at epoch %i and step %i" % (self.start_epoch, self.start_batch), file=log.v3)
    print("using batch size: %i, max seqs: %i" % (self.batch_size, self.max_seqs), file=log.v4)
    print("learning rate control:", self.learning_rate_control, file=log.v4)
    print("pretrain:", self.pretrain, file=log.v4)

    assert self.start_epoch >= 1, "Epochs start at 1."
    final_epoch = self.final_epoch if self.final_epoch != 0 else sys.maxsize
    if self.start_epoch > final_epoch:
      print("No epochs to train, start_epoch: %i, final_epoch: %i" %
            (self.start_epoch, self.final_epoch), file=log.v1)

    self.check_last_epoch()
    self.max_seq_length += (self.start_epoch - 1) * self.inc_seq_length

    epoch = self.start_epoch  # Epochs start at 1.
    rebatch = True
    while epoch <= final_epoch:
      if self.max_seq_length != sys.maxsize:
        if int(self.max_seq_length + self.inc_seq_length) != int(self.max_seq_length):
          print("increasing sequence lengths to", int(self.max_seq_length + self.inc_seq_length), file=log.v3)
          rebatch = True
        self.max_seq_length += self.inc_seq_length
      # In case of random seq ordering, we want to reorder each epoch.
      if self.train_data.init_seq_order(epoch=epoch):
        rebatch = True
      if epoch % self.seq_drop_freq == 0:
        if self.seq_drop > 0.0:
          rebatch = True
      self.epoch = epoch  # type: int

      if 'train' in self.dataset_batches:
        if rebatch:
          del self.dataset_batches['train']
        else:
          print("keeping previous dataset batch order for 'train' dataset", file=log.v4)
      for dataset_name, dataset in self.get_eval_datasets().items():
        if dataset.init_seq_order(epoch=self.epoch):
          if dataset_name in self.dataset_batches:
            del self.dataset_batches[dataset_name]
        else:
          if dataset_name in self.dataset_batches:
            print("keeping previous dataset batch order for %r dataset" % dataset_name, file=log.v4)

      self.init_train_epoch()
      self.train_epoch()

      rebatch = False
      epoch += 1

    if self.start_epoch <= self.final_epoch:  # We did train at least one epoch.
      assert self.epoch
      # Save last model, in case it was not saved yet (depends on save_model_epoch_interval).
      if self.model_filename:
        self.save_model(self.get_epoch_model_filename())

      if self.epoch != self.final_epoch:
        print("Stopped after epoch %i and not %i as planned." % (self.epoch, self.final_epoch), file=log.v3)

    print("Finished training in epoch %i." % self.epoch, file=log.v3)

  def init_train_epoch(self):
    if self.is_pretrain_epoch():
      new_network_desc = self.pretrain.get_network_json_for_epoch(self.epoch)
      self.maybe_init_new_network(new_network_desc)
      self.network.declare_train_params(**self.pretrain.get_train_param_args_for_epoch(self.epoch))
      # Use constant learning rate.
      self.learning_rate = self.pretrain_learning_rate
      self.learning_rate_control.setDefaultLearningRateForEpoch(self.epoch, self.learning_rate)
    elif self.is_first_epoch_after_pretrain():
      # Use constant learning rate.
      self.learning_rate = self.initial_learning_rate
      self.learning_rate_control.setDefaultLearningRateForEpoch(self.epoch, self.learning_rate)
    else:
      self.learning_rate = self.learning_rate_control.getLearningRateForEpoch(self.epoch)

    if not self.is_pretrain_epoch():
      # Train the whole network.
      self.network.declare_train_params()

    self.updater.set_trainable_vars(self.network.get_trainable_params())

    self._maybe_use_better_last_model()

  def _maybe_use_better_last_model(self):
    if not self.config.is_true("use_last_best_model"):
      return
    if self.is_pretrain_epoch():
      return
    opts = self.config.get_of_type("use_last_best_model", dict, default={}).copy()
    if self.epoch % opts.pop("modulo", 1) != 0:
      # Normally we would filter those out. One maybe sensible exception is if the last score was really bad.
      if (self.learning_rate_control.getEpochErrorValue(self.epoch - 1) or 0) \
           <= opts.get("filter_score", float("inf")):
        return
    # Check if the previous epoch model is the best and otherwise take the best last model params.
    last_best_epoch = self.learning_rate_control.getLastBestEpoch(
      last_epoch=self.epoch - 1,
      first_epoch=self.pretrain.get_train_num_epochs() if self.pretrain else 1,
      **opts)
    if last_best_epoch and last_best_epoch != self.epoch - 1:
      print("Last epoch %i (score: %f) is not the optimal model" %
            (self.epoch -1, self.learning_rate_control.getEpochErrorValue(self.epoch -1))
            + " but epoch %i has better score %f (%r), will use that model." %
            (last_best_epoch, self.learning_rate_control.getEpochErrorValue(last_best_epoch),
             self.learning_rate_control.getEpochErrorDict(last_best_epoch)),
            file=log.v2)
      self.load_model(epoch=last_best_epoch)
      self.updater.init_optimizer_vars()  # reset the optimizer vars

  def train_epoch(self):
    print("start", self.get_epoch_str(), "with learning rate", self.learning_rate, "...", file=log.v4)

    if self.epoch == 1 and self.save_epoch1_initial_model:
      epoch0_model_filename = self.epoch_model_filename(self.model_filename, 0, self.is_pretrain_epoch())
      print("save initial epoch1 model", epoch0_model_filename, file=log.v4)
      self.save_model(epoch0_model_filename)

    if 'train' not in self.dataset_batches or not self.train_data.batch_set_generator_cache_whole_epoch():
      self.dataset_batches['train'] = self.train_data.generate_batches(recurrent_net=self.network.recurrent,
                                                                       batch_size=self.batch_size,
                                                                       max_seqs=self.max_seqs,
                                                                       max_seq_length=int(self.max_seq_length),
                                                                       seq_drop=self.seq_drop,
                                                                       shuffle_batches=self.shuffle_batches,
                                                                       used_data_keys=self.network.used_data_keys)
    else:
      self.dataset_batches['train'].reset()
    train_batches = self.dataset_batches['train']

    self.updater.set_learning_rate(self.learning_rate)
    trainer = Runner(engine=self, dataset=self.train_data, batches=train_batches, train=True)
    trainer.run(report_prefix=("pre" if self.is_pretrain_epoch() else "") + "train epoch %s" % self.epoch)

    if not trainer.finalized:
      if trainer.device_crash_batch is not None:  # Otherwise we got an unexpected exception - a bug in our code.
        self.save_model(self.get_epoch_model_filename() + ".crash_%i" % trainer.device_crash_batch)
      print("Trainer not finalized, quitting.", file=log.v1)
      sys.exit(1)

    if any(numpy.isinf(list(trainer.score.values()))) or any(numpy.isnan(list(trainer.score.values()))):
      self.save_model(self.get_epoch_model_filename() + ".broken")
      print("Model seems broken, got inf or nan final score: %s" % trainer.score, file=log.v1)
      sys.exit(1)

    if self.model_filename and (self.epoch % self.save_model_epoch_interval == 0):
      self.save_model(self.get_epoch_model_filename())
    self.learning_rate_control.setEpochError(self.epoch, {"train_score": trainer.score})
    self.learning_rate_control.save()

    print(self.get_epoch_str(), "score:", self.format_score(trainer.score), "elapsed:", hms(trainer.elapsed), end=" ", file=log.v1)
    self.eval_model()

  def format_score(self, score):
    if not score:
      return "None"
    if len(score) == 1:
      return str(list(score.values())[0])
    return " ".join(["%s %s" % (key.split(':')[-1], str(score[key]))
                     for key in sorted(score.keys())])

  def eval_model(self):
    # It's constructed lazily and it will set used_data_keys, so make sure that we have it now.
    self.network.get_all_errors()
    eval_dump_str = []
    for dataset_name, dataset in self.get_eval_datasets().items():
      if dataset_name not in self.dataset_batches or not dataset.batch_set_generator_cache_whole_epoch():
        self.dataset_batches[dataset_name] = dataset.generate_batches(
          recurrent_net=self.network.recurrent,
          batch_size=self.batch_size,
          max_seqs=self.max_seqs,
          max_seq_length=(int(self.max_seq_length) if dataset_name == 'dev' else sys.maxsize),
          used_data_keys=self.network.used_data_keys)
      else:
        self.dataset_batches[dataset_name].reset()
      tester = Runner(engine=self, dataset=dataset, batches=self.dataset_batches[dataset_name], train=False)
      tester.run(report_prefix=self.get_epoch_str() + " eval")
      assert tester.finalized
      eval_dump_str += [" %s: score %s error %s" % (
                        dataset_name, self.format_score(tester.score), self.format_score(tester.error))]
      if dataset_name == "dev":
        self.learning_rate_control.setEpochError(self.epoch, {"dev_score": tester.score, "dev_error": tester.error})
        self.learning_rate_control.save()
    print(" ".join(eval_dump_str).strip(), file=log.v1)

  def check_last_epoch(self):
    if self.start_epoch == 1:
      return
    self.epoch = self.start_epoch - 1
    if self.learning_rate_control.need_error_info:
      if self.dev_data:
        if "dev_score" not in self.learning_rate_control.getEpochErrorDict(self.epoch):
          # This can happen when we have a previous model but did not test it yet.
          print("Last epoch model not yet evaluated on dev. Doing that now.", file=log.v4)
          self.eval_model()

  def get_all_merged_summaries(self):
    """
    :return: merged summaries, serialized string
    :rtype: tf.Tensor
    """
    # Note: This assumes that the summaries never change.
    # Both both training and evaluation on the CV dataset, this is the case.
    if self._merge_all_summaries is None:
      self._merge_all_summaries = tf.summary.merge_all()
    return self._merge_all_summaries

  def check_uninitialized_vars(self):
    """
    All vars in TF which are controlled by us should also have been initialized by us.
    We also take care about the optimizer slot variables.
    However, TF can still create other vars which we do not know about.
    E.g. the Adam optimizer creates the beta1_power/beta2_power vars (which are no slot vars).
    Here, we find all remaining uninitialized vars, report about them and initialize them.
    """
    if self._checked_uninitialized_vars:
      return
    with tf.name_scope("check_uninitialized_vars"):
      # Like tf.report_uninitialized_variables().
      var_list = tf.global_variables() + tf.local_variables()
      # Get a 1-D boolean tensor listing whether each variable is initialized.
      var_mask = tf.logical_not(tf.stack(
        [tf.is_variable_initialized(v) for v in var_list])).eval(session=self.tf_session)
      assert len(var_mask) == len(var_list)
      uninitialized_vars = [v for (v, mask) in zip(var_list, var_mask) if mask]
      if uninitialized_vars:
        print("Note: There are still these uninitialized variables: %s" % [v.name for v in uninitialized_vars], file=log.v3)
        self.tf_session.run(tf.variables_initializer(uninitialized_vars))
      self._checked_uninitialized_vars = True

  def get_specific_feed_dict(self, dataset, seq_idx):
    """
    :param Dataset.Dataset dataset:
    :param int seq_idx:
    :return: feed_dict for self.tf_session.run()
    :rtype: dict[str,numpy.ndarray]
    """
    # No Runner instance here but a very simplified version of Runner.run().
    # First we need a custom DataProvider with a custom BatchSetGenerator
    # which will yield only one single batch for the provided sequence idx.
    batch = Batch()
    batch.init_with_one_full_sequence(seq_idx=seq_idx, dataset=dataset)
    batch_generator = iter([batch])
    batches = BatchSetGenerator(dataset, generator=batch_generator)
    from TFDataPipeline import FeedDictDataProvider
    data_provider = FeedDictDataProvider(
      tf_session=self.tf_session, extern_data=self.network.extern_data,
      data_keys=self.network.used_data_keys,
      dataset=dataset, batches=batches)
    feed_dict = data_provider.get_feed_dict(single_threaded=True)
    return feed_dict

  def run_single(self, dataset, seq_idx, output_dict, ext_feed_dict=None):
    """
    :param Dataset.Dataset dataset:
    :param int seq_idx:
    :param dict[str,tf.Tensor] output_dict: key -> tf.Tensor
    :param dict[tf.Tensor,numpy.ndarray] ext_feed_dict:
    :return: output_dict but values evaluated
    :rtype: dict[str,numpy.ndarray]
    """
    feed_dict = self.get_specific_feed_dict(dataset=dataset, seq_idx=seq_idx)
    if ext_feed_dict:
      feed_dict.update(ext_feed_dict)
    self.check_uninitialized_vars()  # Maybe some new uninitialized vars. Last check.
    return self.tf_session.run(output_dict, feed_dict=feed_dict)

  def _get_output_layer(self, output_layer_name=None):
    """
    :param str|None output_layer_name: e.g. "output". if not set, will read from config "forward_output_layer"
    :rtype: TFNetworkLayer.LayerBase
    """
    if not output_layer_name:
      output_layer_name = self.config.value("forward_output_layer", self.network.get_default_output_layer_name())
      assert output_layer_name, "output layer not defined. set forward_output_layer in config"
    assert output_layer_name in self.network.layers, "output layer %r not found" % output_layer_name
    return self.network.layers[output_layer_name]

  def forward_single(self, dataset, seq_idx, output_layer_name=None):
    """
    :param Dataset.Dataset dataset:
    :param int seq_idx:
    :param str|None output_layer_name: e.g. "output". if not set, will read from config "forward_output_layer"
    :return: numpy array, output in time major format (time,dim)
    :rtype: numpy.ndarray
    """
    output_data = self._get_output_layer(output_layer_name).output
    out = output_data.get_placeholder_as_time_major()
    out_d = self.run_single(dataset=dataset, seq_idx=seq_idx, output_dict={"out": out})
    output_value = out_d["out"]
    assert output_value.shape[1] == 1  # batch-dim
    return output_value[:, 0]  # remove batch-dim

  def forward_to_hdf(self, data, output_file, combine_labels='', batch_size=0):
    """
    Is aiming at recreating the same interface and output as :func:`Engine.forward_to_hdf`.
    See also :func:`EngineTask.HDFForwardTaskThread` and :func:`hdf_dump_from_dataset` in the hdf_dump.py tool.

    :param Dataset data:
    :param str output_file:
    :param str combine_labels: ignored at the moment
    :param int batch_size:
    """
    import h5py
    from Util import hdf5_strings

    output_layer = self._get_output_layer()
    target = self.network.get_default_target()

    assert output_file
    assert not os.path.exists(output_file)
    print("Forwarding to HDF file: %s" % output_file, file=log.v2)
    cache = h5py.File(output_file, "w")
    cache.attrs['numTimesteps'] = 0
    cache.attrs['inputPattSize'] = data.num_inputs
    cache.attrs['numDims'] = 1
    cache.attrs['numLabels'] = data.num_outputs[target]
    cache.attrs['numSeqs'] = 0
    if target in data.labels:
      hdf5_strings(cache, 'labels', data.labels[target])
    else:
      cache.create_dataset('labels', (0,), dtype="S5")

    datasets = {}  # type: dict[str,h5py.Dataset]
    tags = []  # type: list[str]
    seq_lengths = cache.create_dataset("seqLengths", (0,2), dtype='i', maxshape=(None,2))

    def insert_h5_inputs(name, raw_data):
      """
      Inserts a record into the hdf5-file.
      Resizes if necessary.

      :param str name:
      :param numpy.ndarray raw_data: shape=(time,data)
      """
      assert len(raw_data.shape) == 2
      if name not in datasets:
        datasets[name] = cache.create_dataset(name, raw_data.shape, raw_data.dtype, maxshape=tuple(None for _ in raw_data.shape))
      else:
        old_shape = datasets[name].shape
        datasets[name].resize((old_shape[0] + raw_data.shape[0],) + old_shape[1:])
      # append raw data to dataset
      datasets[name][cache.attrs['numTimesteps']:, 0:] = raw_data
      cache.attrs['numTimesteps'] += raw_data.shape[0]
      cache.attrs['numSeqs'] += 1

    def extra_fetches_cb(inputs, seq_len, seq_tag):
      """
      Insert each batch into the output_file (hdf).

      :param numpy.ndarray inputs: shape=(n_batch,time,data)
      :param list[int] seq_len: sequence lengths
      :param list[str] seq_tag: sequence tags of length n_batch
      """
      n_batch = len(seq_len)
      assert n_batch == len(seq_tag)
      assert n_batch == inputs.shape[0]

      seqlen_offset = seq_lengths.shape[0]
      seq_lengths.resize(seqlen_offset + n_batch, axis=0)
      for i in range(n_batch):
        tags.append(seq_tag[i])
        seq_lengths[seqlen_offset + i] = seq_len[i]
        insert_h5_inputs('inputs', inputs[i][:seq_len[i]])

    batches = data.generate_batches(
      recurrent_net=self.network.recurrent,
      batch_size=batch_size,
      max_seqs=self.max_seqs,
      used_data_keys=self.network.used_data_keys)
    forwarder = Runner(
      engine=self, dataset=data, batches=batches,
      train=False, eval=False,
      extra_fetches={
        'inputs': output_layer.output.get_placeholder_as_batch_major(),
        "seq_len": output_layer.output.get_sequence_lengths(),
        "seq_tag": self.network.get_seq_tags(),
      },
      extra_fetches_callback=extra_fetches_cb)
    forwarder.run(report_prefix=self.get_epoch_str() + " forward")
    if not forwarder.finalized:
      print("Error happened. Exit now.")
      sys.exit(1)

    max_tag_len = max([len(d) for d in tags])
    cache.create_dataset('seqTags', shape=(len(tags),), dtype="S%i" % (max_tag_len + 1))
    for i, tag in enumerate(tags):
      cache['seqTags'][i] = numpy.array(tag, dtype="S%i" % (max_tag_len + 1))
    cache.close()

  def analyze(self, data, statistics):
    """
    :param Dataset.Dataset data:
    :param list[str]|None statistics: ignored at the moment
    :return: nothing, will print everything to log.v1
    """
    print("Analyze with network on %r." % data, file=log.v1)

    if "analyze" not in self.network.layers:
      from TFNetworkLayer import FramewiseStatisticsLayer
      assert self.config.has("sil_label_idx")
      self.network.add_layer(
        name="analyze", layer_class=FramewiseStatisticsLayer,
        sil_label_idx=self.config.int("sil_label_idx", 0),
        sources=self.network.get_output_layers())

    # It's constructed lazily and it will set used_data_keys, so make sure that we have it now.
    self.network.get_all_errors()

    batch_size = self.config.int('batch_size', 1)
    max_seqs = self.config.int('max_seqs', -1)
    max_seq_length = self.config.float('max_seq_length', 0)
    if max_seq_length <= 0:
      max_seq_length = sys.maxsize

    batches = data.generate_batches(
      recurrent_net=self.network.recurrent,
      batch_size=batch_size,
      max_seqs=max_seqs,
      max_seq_length=max_seq_length,
      used_data_keys=self.network.used_data_keys)
    analyzer = Runner(engine=self, dataset=data, batches=batches, train=False)
    analyzer.run(report_prefix=self.get_epoch_str() + " analyze")

    print("Finished analyzing of the dataset %r." % data, file=log.v1)
    print("elapsed:", hms(analyzer.elapsed), file=log.v1)
    print("num mini-batches:", analyzer.num_steps, file=log.v1)
    print("total num_frames:", analyzer.num_frames_accumulated, file=log.v1)
    print("score:", self.format_score(analyzer.score), file=log.v1)
    print("error:", self.format_score(analyzer.error), file=log.v1)
    for k, v in sorted(analyzer.stats.items()):
      if k.startswith("stats:"):
        print("%s:" % k, v, file=log.v1)
    print("That are all collected stats.", file=log.v1)

    if not analyzer.finalized:
      print("WARNING: Did not finished through the whole epoch.", file=log.v1)
      sys.exit(1)

  def search(self, dataset, do_eval=True, output_layer_name="output", output_file=None):
    """
    :param Dataset.Dataset dataset:
    :param bool do_eval: calculate errors. can only be done if we have the reference target
    :param str output_layer_name:
    :param str output_file:
    """
    print("Search with network on %r." % dataset, file=log.v1)
    if not self.use_search_flag or not self.network or self.use_dynamic_train_flag:
      self.use_search_flag = True
      # At the moment this is probably not intended to use search with train flag.
      # Also see LayerBase._post_init_output() about setting size_placeholder to the target seq len,
      # so you would have have_known_seq_len=True in the RecLayer, with the given target seq len.
      self.use_dynamic_train_flag = False
      if self.network:
        print("Reinit network with search flag.", file=log.v3)
      self.init_network_from_config(self.config)
    if do_eval:
      # It's constructed lazily and it will set used_data_keys, so make sure that we have it now.
      self.network.get_all_errors()
    if output_file:
      dataset.seq_ordering = "default"  # enforce order as-is, so that the order in the written file corresponds
    dataset.init_seq_order(epoch=self.epoch)
    batches = dataset.generate_batches(
      recurrent_net=self.network.recurrent,
      batch_size=self.config.int('batch_size', 1),
      max_seqs=self.config.int('max_seqs', -1),
      max_seq_length=int(self.config.float('max_seq_length', 0)),
      used_data_keys=self.network.used_data_keys)

    output_layer = self.network.layers[output_layer_name]
    out_beam_size = output_layer.output.beam_size
    if out_beam_size is None:
      print("Given output %r is after decision (no beam)." % output_layer, file=log.v1)
    else:
      print("Given output %r has beam size %i." % (output_layer, out_beam_size), file=log.v1)
    target_key = "classes"

    if output_file:
      assert dataset.can_serialize_data(target_key)
      assert not os.path.exists(output_file)
      print("Will write outputs to: %s" % output_file, file=log.v2)
      output_file = open(output_file, "w")

    def extra_fetches_callback(seq_idx, seq_tag, output, targets=None):
      """
      :param list[int] seq_idx: of length batch (without beam)
      :param list[str] seq_tag: of length batch (without beam)
      :param list[numpy.ndarray] output: of length batch (with beam)
      :param list[numpy.ndarray] targets: of length batch (without beam)
      """
      n_batch = len(seq_idx)  # without beam
      assert n_batch == len(seq_tag)
      assert n_batch * (out_beam_size or 1) == len(output)
      if output_layer.output.dim == 256 and output_layer.output.sparse:
        # Interpret output as bytes/utf8-string.
        output = [bytearray(o).decode("utf8") for o in output]
      for i in range(len(seq_idx)):
        if out_beam_size is None:
          print("seq_idx: %i, seq_tag: %r, output: %r" % (seq_idx[i], seq_tag[i], output[i]), file=log.v1)
          out_idx = i
        else:
          print("seq_idx: %i, seq_tag: %r, outputs: %r" % (
            seq_idx[i], seq_tag[i], output[i * out_beam_size:(i + 1)*out_beam_size]), file=log.v1)
          out_idx = i * out_beam_size
        if target_key and dataset.can_serialize_data(target_key):
          print("  hyp:", dataset.serialize_data(key=target_key, data=output[out_idx]), file=log.v1)
          print("  ref:", dataset.serialize_data(key=target_key, data=targets[out_idx]), file=log.v1)
        if output_file:
          output_file.write("%s\n" % dataset.serialize_data(key=target_key, data=output[out_idx]))
          output_file.flush()

    runner = Runner(
      engine=self, dataset=dataset, batches=batches, train=False, eval=do_eval,
      extra_fetches={
        "output": output_layer,
        "seq_idx": self.network.get_extern_data("seq_idx", mark_data_key_as_used=True),
        "seq_tag": self.network.get_extern_data("seq_tag", mark_data_key_as_used=True),
        "targets": self.network.get_extern_data(target_key, mark_data_key_as_used=True)},
      extra_fetches_callback=extra_fetches_callback)
    runner.run(report_prefix=self.get_epoch_str() + " search")
    if not runner.finalized:
      print("Error happened. Exit now.")
      sys.exit(1)
    print("Search done. Final: score %s error %s" % (
      self.format_score(runner.score), self.format_score(runner.error)), file=log.v1)
    if output_file:
      output_file.close()

  def compute_priors(self, dataset, config=None):
    """
    :param Dataset dataset:
    :param Config.Config config:
    """
    assert isinstance(dataset, Dataset)
    if config:
      assert config is self.config

    output_layer = self._get_output_layer()
    assert config.has('output_file'), 'output_file for priors numbers should be provided'
    output_file = config.value('output_file', '')
    assert not os.path.exists(output_file), "Already existing output file %r." % output_file
    print("Compute priors, using output layer %r, writing to %r." % (output_layer, output_file), file=log.v2)

    class Accumulator(object):
      """
      Also see PriorEstimationTaskThread for reference.
      """

      def __init__(self):
        self.sum_posteriors = numpy.zeros(int(output_layer.output.dim))
        self.seq_len = 0

      def __call__(self, outputs):
        """
        Called via extra_fetches_callback from the Runner.

        :param numpy.ndarray outputs: shape=(time,data)|(time,), depending if dense or sparse, flattened over batches
        """
        seq_len = outputs.shape[0]
        if output_layer.output.sparse:
          assert outputs.shape == (seq_len,)
        else:
          assert outputs.shape == (seq_len, output_layer.output.dim)
        if output_layer.output.sparse:
          from Util import class_idx_seq_to_1_of_k
          outputs = class_idx_seq_to_1_of_k(outputs, num_classes=output_layer.output.dim)
        self.sum_posteriors += numpy.sum(outputs, axis=0)
        self.seq_len += seq_len

    accumulator = Accumulator()
    batch_size = config.int('batch_size', 1)
    max_seqs = config.int('max_seqs', -1)
    epoch = config.int('epoch', 1)
    max_seq_length = config.float('max_seq_length', 0)
    if max_seq_length <= 0:
      max_seq_length = sys.maxsize
    dataset.init_seq_order(epoch=epoch)
    batches = dataset.generate_batches(
      recurrent_net=self.network.recurrent,
      batch_size=batch_size,
      max_seq_length=max_seq_length,
      max_seqs=max_seqs,
      used_data_keys=self.network.used_data_keys)
    forwarder = Runner(
      engine=self, dataset=dataset, batches=batches,
      train=False, eval=False,
      extra_fetches={
        'outputs': output_layer.output.get_placeholder_flattened()
      },
      extra_fetches_callback=accumulator)
    forwarder.run(report_prefix=self.get_epoch_str() + " forward")
    if not forwarder.finalized:
      print("Error happened. Exit now.")
      sys.exit(1)

    average_posterior = accumulator.sum_posteriors / accumulator.seq_len
    avg_sum = numpy.sum(average_posterior)
    assert numpy.isfinite(avg_sum)
    print("Prior sum in std-space (should be close to 1.0):", avg_sum, file=log.v1)
    log_average_posterior = numpy.log(average_posterior)
    with open(output_file, 'w') as f:
      numpy.savetxt(f, log_average_posterior, delimiter=' ')
    print("Saved prior in %r in +log space." % output_file, file=log.v1)


def get_global_engine():
  """
  Similar as get_global_config().

  :rtype: Engine
  """

  import sys
  main_mod = sys.modules["__main__"]  # should be rnn.py
  if isinstance(getattr(main_mod, "engine", None), Engine):
    return main_mod.engine
  # Maybe __main__ is not rnn.py, or config not yet loaded.
  # Anyway, try directly. (E.g. for SprintInterface.)
  import rnn
  assert isinstance(rnn.engine, Engine)  # no other option anymore
  return rnn.engine
