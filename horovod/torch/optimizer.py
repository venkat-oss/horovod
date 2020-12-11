# Copyright 2019 Uber Technologies, Inc. All Rights Reserved.
# Modifications copyright Microsoft
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import os
import warnings
import io
from contextlib import contextmanager
import pdb
import torch
from torch import Tensor as TT
from torch import count_nonzero
import numpy as np
import cloudpickle
from scipy.sparse import dok_matrix
from horovod.torch.compression import Compression
from horovod.torch.mpi_ops import allreduce_async_
from horovod.torch.mpi_ops import allgather_async
from horovod.torch.mpi_ops import synchronize
from horovod.torch.mpi_ops import size
from horovod.torch.mpi_ops import Average, Adasum, Sum
from horovod.torch.mpi_ops import rocm_built


class _DistributedOptimizer(torch.optim.Optimizer):
    def __init__(self, params, named_parameters, compression,
                 backward_passes_per_step=1, op=Average,
                 gradient_predivide_factor=1.0,
                 num_groups=0):
        super(self.__class__, self).__init__(params)
        self._compression = compression

        if named_parameters is not None:
            named_parameters = list(named_parameters)
        else:
            named_parameters = [('allreduce.noname.%s' % i, v)
                                for param_group in self.param_groups
                                for i, v in enumerate(param_group['params'])]
        # make sure that named_parameters are tuples
        if any([not isinstance(p, tuple) for p in named_parameters]):
            raise ValueError('named_parameters should be a sequence of '
                             'tuples (name, parameter), usually produced by '
                             'model.named_parameters().')

        dups = _DistributedOptimizer.find_duplicates([k for k, _ in named_parameters])
        if len(dups) > 0:
            raise ValueError('Parameter names in named_parameters must be unique. '
                             'Found duplicates: %s' % ', '.join(dups))

        all_param_ids = {id(v)
                         for param_group in self.param_groups
                         for v in param_group['params']}
        named_param_ids = {id(v) for k, v in named_parameters}
        unnamed_param_ids = all_param_ids - named_param_ids
        if len(unnamed_param_ids):
            raise ValueError('named_parameters was specified, but one or more model '
                             'parameters were not named. Python object ids: '
                             '%s' % ', '.join(str(id) for id in unnamed_param_ids))

        self._parameter_names = {v: k for k, v in sorted(named_parameters)}
        self.backward_passes_per_step = backward_passes_per_step
        self._allreduce_delay = {v: self.backward_passes_per_step
                                 for _, v in sorted(named_parameters)}
        self.op = op
        self.gradient_predivide_factor = gradient_predivide_factor
        self._handles = {}
        self._grad_accs = []
        self._requires_update = set()
        self._synchronized = False
        self._should_synchronize = True
        self._num_groups = 0
        self._p_to_group = {}
        self._group_counts = {}
        if size() > 1 or os.environ.get('HOROVOD_ELASTIC') == '1':
            self._register_hooks()

    def load_state_dict(self, *args, **kwargs):
        self._handles = {}
        self._synchronized = False
        self._should_synchronize = True
        for p in self._allreduce_delay:
            self._allreduce_delay[p] = self.backward_passes_per_step
        super(self.__class__, self).load_state_dict(*args, **kwargs)

    @staticmethod
    def find_duplicates(lst):
        seen = set()
        dups = set()
        for el in lst:
            if el in seen:
                dups.add(el)
            seen.add(el)
        return dups

    def set_backward_passes_per_step(self, passes):
        self.backward_passes_per_step = passes
        for p in self._allreduce_delay:
            self._allreduce_delay[p] = self.backward_passes_per_step

    def _register_hooks(self):

        if self._num_groups > 0:
            p_list = []
            # Get list of parameters with grads
            for param_group in self.param_groups:
                for p in param_group['params']:
                    if p.requires_grad:
                        p_list.append(p)

            # To ensure parameter order and group formation is consistent, broadcast p_list order
            # from rank 0 and use for every worker
            p_list_names = [self._parameter_names.get(p) for p in p_list]
            p_list_names = broadcast_object(p_list_names, root_rank=0)
            p_list = sorted(p_list, key=lambda p : p_list_names.index(self._parameter_names.get(p)))

            # Form groups
            p_groups = split_list(p_list, self._num_groups)
            p_groups = [tuple(p) for p in p_groups]
            for group in p_groups:
              for p in group:
                self._p_to_group[p] = group
              self._group_counts[group] = 0

        for param_group in self.param_groups:
            for p in param_group['params']:
                if p.requires_grad:
                    p.grad = p.data.new(p.size()).zero_()
                    self._requires_update.add(p)
                    p_tmp = p.expand_as(p)
                    grad_acc = p_tmp.grad_fn.next_functions[0][0]
                    grad_acc.register_hook(self._make_hook(p))
                    self._grad_accs.append(grad_acc)

    def _newdirective_grad_async(self, p):
        # pdb.set_trace()
        name = self._parameter_names.get(p)
        tensor = p.grad
        tensor_compressed, ctx = self._compression.compress(tensor)

        if self.op == Average:
           # Split average operation across pre/postscale factors
           # C++ backend will apply additional 1 / size() factor to postscale_factor for op == Average.
            prescale_factor = 1.0 / self.gradient_predivide_factor
            postscale_factor = self.gradient_predivide_factor
        else:
            prescale_factor = 1.0
            postscale_factor = 1.0

        sparsity = (TT.numel(tensor_compressed) - count_nonzero(tensor_compressed).item()) / TT.numel(tensor_compressed)
        empty_tensor = torch.ones((1,), dtype=torch.int8)
        #vote for allgather
        vote_tensor = empty_tensor.new_tensor([1]) if sparsity<0.75 else empty_tensor.new_tensor([0])
        # get all votes
        voted_tensor = synchronize(allgather_async(vote_tensor))
        consensus =  (TT.numel(voted_tensor) - count_nonzero(voted_tensor).item()) / TT.numel(voted_tensor)
        mpi_type = None

        # if consensus>0.5:
        if False:
            # print(f'Param Name : {name} -> AG voted_tensor : {voted_tensor}')
            # print(f'Tensor : {tensor_compressed}')
            mpi_type = 'AG'
            # uses the same underlying storage
            # print('Step 0_0')
            # numpy_fmt = tensor_compressed.numpy()
            # np_data = np.argwhere(numpy_fmt)
            # np_row, np_col = numpy_fmt.nonzero()
            # print('Step 0_1')
            #lot of time b/w steps
            # matrix = np.asmatrix(numpy_fmt)
            # matrix_dtype = numpy_fmt.dtype
            # dok  = dok_matrix(matrix, dtype=matrix.dtype)
            # print('Step 0_2')
            # name = type(dok).__name__
            # print('Step 0')
            # b = io.BytesIO()
            # print('Step 1_0')
            #lot of time b/w steps
            # cloudpickle.dump(dok, b)
            # print('Step 1')
            # t = torch.ByteTensor(bytearray(b.getvalue()))
            # sz = torch.IntTensor([t.shape[0]])
            # print('Step 2')
            # sizes = allgather_async(sz, name=name + '.sz')
            # gathered = allgather_async(t, name=name + '.t')
            sizes = None
            # print(f'tensor_compressed {tensor_compressed.size()}')
            gathered = allgather_async(tensor_compressed, name=name)
            # print('Step 3')
            handle = (sizes, gathered)
            # print('AG Handle generated')
        else:
            mpi_type = 'AR'
            handle = allreduce_async_(tensor_compressed, name=name, op=self.op,
                                  prescale_factor=prescale_factor,
                                  postscale_factor=postscale_factor)
            # print('AR Handle generated')

        return handle, ctx, mpi_type

    def _allreduce_grad_async(self, p):
        name = self._parameter_names.get(p)
        tensor = p.grad
        # topk_num = math.floor(tensor.shape[-1] * 0.2)
        # print(f' numel {torch.numel(tensor)} - topk_num {topk_num}')
        # print(f'{torch.topk(tensor,topk_num)} - {topk_num}')
        # tensor_topk, indices = torch.topk(tensor,topk_num)
        # tensor_topk = torch.zeros_like(tensor).scatter_(len(tensor.shape)-1, indices, tensor_topk)
        # print(f'{tensor} - {tensor_topk}')
        tensor_compressed, ctx = self._compression.compress(tensor)
        print(tensor_compressed)
        if self.op == Average:
           # Split average operation across pre/postscale factors
           # C++ backend will apply additional 1 / size() factor to postscale_factor for op == Average.
            prescale_factor = 1.0 / self.gradient_predivide_factor
            postscale_factor = self.gradient_predivide_factor
        else:
            prescale_factor = 1.0
            postscale_factor = 1.0

        handle = allreduce_async_(tensor_compressed, name=name, op=self.op,
                                  prescale_factor=prescale_factor,
                                  postscale_factor=postscale_factor)
        return handle, ctx

    def _grouped_allreduce_grad_async(self, ps):
        name = self._parameter_names.get(ps[0])
        tensors_compressed, ctxs = zip(*[self._compression.compress(p.grad) for p in ps])

        handle = grouped_allreduce_async_(tensors_compressed, name=name, op=self.op)
        return handle, ctxs

    def _make_hook(self, p):
        def hook(*ignore):
            if p in self._handles and self._handles[p][0] is not None:
                if self._allreduce_delay[p] <= 0:
                    raise AssertionError(
                        "Gradients were computed more than "
                        "backward_passes_per_step times before call "
                        "to step(). Increase backward_passes_per_step to "
                        "accumulate gradients locally.")
            assert not p.grad.requires_grad
            assert self._allreduce_delay[p] > 0
            handle, ctx = None, None
            self._allreduce_delay[p] -= 1
            if self._allreduce_delay[p] == 0:
                if self._num_groups > 0:
                    group = self._p_to_group[p]
                    self._group_counts[group] += 1
                    if self._group_counts[group] == len(group):
                        handle, ctxs = self._grouped_allreduce_grad_async(group)
                        self._handles[group] = (handle, ctxs)
                        # Remove any None entries from previous no-op hook calls
                        for gp in group:
                            self._handles.pop(gp, None)
                        self._group_counts[group] = 0
                        return
                else:
                    handle, ctx, mpi_type = self._newdirective_grad_async(p)
            self._handles[p] = (handle, ctx, mpi_type)
        return hook

    def synchronize_original(self):
        completed = set()
        for x in self._handles.keys():
          completed.update(x) if isinstance(x, tuple) else completed.add(x)
        missing_p = self._requires_update - completed
        for p in missing_p:
            handle, ctx = self._allreduce_grad_async(p)
            self._handles[p] = (handle, ctx)

        for p, (handle, ctx) in self._handles.items():
            if handle is None:
                handle, ctx = self._allreduce_grad_async(p)
                self._handles[p] = (handle, ctx)
        for p, (handle, ctx) in self._handles.items():

            if isinstance(p, tuple):
                # This was a grouped result, need to unpack
                outputs = synchronize(handle)
                for gp, output, gctx in zip(p, outputs, ctx):
                    self._allreduce_delay[gp] = self.backward_passes_per_step
                    gp.grad.set_(self._compression.decompress(output, gctx))
            else:
                output = synchronize(handle)
                self._allreduce_delay[p] = self.backward_passes_per_step
                p.grad.set_(self._compression.decompress(output, ctx))
        self._handles.clear()

        self._synchronized = True

    def synchronize(self):
        completed = set()
        for x in self._handles.keys():
          completed.update(x) if isinstance(x, tuple) else completed.add(x)
        missing_p = self._requires_update - completed
        for p in missing_p:
            handle, ctx, mpi_type = self._newdirective_grad_async(p)
            self._handles[p] = (handle, ctx, mpi_type)

        for p, (handle, ctx, mpi_type) in self._handles.items():
            if handle is None:
                handle, ctx, mpi_type = self._newdirective_grad_async(p)
                self._handles[p] = (handle, ctx, mpi_type)
        for p, (handle, ctx, mpi_type) in self._handles.items():

            if mpi_type=='AG':
                # print('AG Output creating')
                h_sizes, h_gathered = handle
                # sizes = synchronize(h_sizes).numpy()
                # gathered = synchronize(h_gathered).numpy()
                gathered = synchronize(h_gathered)


                def load(byte_array):
                    buf = io.BytesIO(byte_array.tobytes())
                    return cloudpickle.load(buf)

                def select(i):
                    start = sum(sizes[:i])
                    end = start + sizes[i]
                    return gathered[start:end]

                # output = [load(select(i)) for i in range(size())] 
                # averaging the summed gradients
                # output = sum(output)/size()
                # convert to torch tensor 
                # output = torch.tensor(output.astype(np.float32).toarray())
                # list_of_tensors = []
                # print(f'Before Split {gathered.size()}')
                dim_0_size = list(gathered.size())[0]
                # print(f'dim_0_size {dim_0_size}')
                split_size = int(dim_0_size/size())
                # print(f'split_size {split_size}')
                gathered = torch.split(gathered,split_size)
                # for e in gathered:
                #     print(f'Loop {e.size()}')
                #     list_of_tensors.append(torch.FloatTensor(e))
                output = sum(gathered)
                # print(f'After Sum {output.size()}')
                output = output/size()
                # print(output)
                # print('AG Output created')
            else :
                # mpi_type=='AR' here
                output = synchronize(handle)
            
            self._allreduce_delay[p] = self.backward_passes_per_step
            p.grad.set_(self._compression.decompress(output, ctx))
        self._handles.clear()

        self._synchronized = True

    @contextmanager
    def skip_synchronize(self):
        """
        A context manager used to specify that optimizer.step() should
        not perform synchronization.

        It's typically used in a following pattern:

        .. code-block:: python

            optimizer.synchronize()
            with optimizer.skip_synchronize():
                optimizer.step()
        """
        self._should_synchronize = False
        try:
            yield
        finally:
            self._should_synchronize = True

    def step(self, closure=None):
        if self._should_synchronize:
            if self._synchronized:
                warnings.warn("optimizer.step() called without "
                              "optimizer.skip_synchronize() context after "
                              "optimizer.synchronize(). This can cause training "
                              "slowdown. You may want to consider using "
                              "optimizer.skip_synchronize() context if you use "
                              "optimizer.synchronize() in your code.")
            self.synchronize()
        self._synchronized = False
        return super(self.__class__, self).step(closure)

    def zero_grad(self):
        if self._handles:
            raise AssertionError("optimizer.zero_grad() was called after loss.backward() "
                                 "but before optimizer.step() or optimizer.synchronize(). "
                                 "This is prohibited as it can cause a race condition.")
        return super(self.__class__, self).zero_grad()


class _DistributedAdasumOptimizer(torch.optim.Optimizer):
    def __init__(self, params, named_parameters, compression,
                 backward_passes_per_step=1):
        super(self.__class__, self).__init__(params)

        self._compression = compression

        if named_parameters is not None:
            named_parameters = list(named_parameters)
        else:
            named_parameters = [('allreduce.noname.%s' % i, v)
                                for param_group in self.param_groups
                                for i, v in enumerate(param_group['params'])]

        # make sure that named_parameters are tuples
        if any([not isinstance(p, tuple) for p in named_parameters]):
            raise ValueError('named_parameters should be a sequence of '
                             'tuples (name, parameter), usually produced by '
                             'model.named_parameters().')

        dups = _DistributedOptimizer.find_duplicates([k for k, _ in named_parameters])
        if len(dups) > 0:
            raise ValueError('Parameter names in named_parameters must be unique. '
                             'Found duplicates: %s' % ', '.join(dups))

        all_param_ids = {id(v)
                         for param_group in self.param_groups
                         for v in param_group['params']}
        named_param_ids = {id(v) for k, v in named_parameters}
        unnamed_param_ids = all_param_ids - named_param_ids
        if len(unnamed_param_ids):
            raise ValueError('named_parameters was specified, but one or more model '
                             'parameters were not named. Python object ids: '
                             '%s' % ', '.join(str(id) for id in unnamed_param_ids))

        self._parameter_names = {v: k for k, v in sorted(named_parameters)}
        self.backward_passes_per_step = backward_passes_per_step
        self._allreduce_delay = {v: self.backward_passes_per_step
                                 for _, v in sorted(named_parameters)}
        self._handles = {}
        self._grad_accs = []
        self._requires_update = set()
        self._synchronized = False
        self._should_synchronize = True

        self._starting_models = {
            p : torch.zeros_like(p, requires_grad=False)
            for _, p in named_parameters
        }

        self._register_hooks()

    def set_backward_passes_per_step(self, passes):
        self.backward_passes_per_step = passes
        for p in self._allreduce_delay:
            self._allreduce_delay[p] = self.backward_passes_per_step

    def _register_hooks(self):
        for param_group in self.param_groups:
            for p in param_group['params']:
                if p.requires_grad:
                    p.grad = p.data.new(p.size()).zero_()
                    self._requires_update.add(p)
                    p_tmp = p.expand_as(p)
                    grad_acc = p_tmp.grad_fn.next_functions[0][0]
                    grad_acc.register_hook(self._make_hook(p))
                    self._grad_accs.append(grad_acc)

    def _allreduce_grad_async(self, p):
        # Delta optimizer implements this logic:
        #  start = current.copy()
        #  step() -> computes 'current - \alpha.f(g)' where f is
        #            optimizer logic and g is the gradient
        #  delta = current-start
        #  allreduce_(delta)
        #  start += delta
        #  current = start
        # In order to suppport this logic using function hook to improve performance,
        # we do:
        # delta = (start - \alpha.f(g)) - start
        #       = -\alpha.f(g)
        # set start to zero and step computes -\alpha.f(g)
        # where f is the underlying optimizer logic

        name = self._parameter_names.get(p)
        start = self._starting_models[p]

        stashed_params = []
        for group in self.param_groups:
            stashed_params.append(group['params'])
            # only want to step on p
            if any([p is v for v in group['params']]):
                group['params'] = [p]
            else:
                group['params'] = []

        start.data.copy_(p)

        super(self.__class__, self).step()

        # compute delta = curr - start
        p.data.sub_(start)

        # allreduce as before
        tensor_compressed, ctx = self._compression.compress(p)
        handle = allreduce_async_(tensor_compressed.data, name=name, op=Adasum)

        # reset stashed parameters
        for stashed, group in zip(stashed_params, self.param_groups):
            group['params'] = stashed

        return handle, ctx

    def _make_hook(self, p):
        def hook(*ignore):
            if p in self._handles and self._handles[p][0] is not None:
                if self._allreduce_delay[p] <= 0:
                    raise AssertionError(
                        "Gradients were computed more than "
                        "backward_passes_per_step times before call "
                        "to step(). Increase backward_passes_per_step to "
                        "accumulate gradients locally.")
            assert not p.grad.requires_grad
            assert self._allreduce_delay[p] > 0
            handle, ctx = None, None
            self._allreduce_delay[p] -= 1
            if self._allreduce_delay[p] == 0:
                handle, ctx = self._allreduce_grad_async(p)
            self._handles[p] = (handle, ctx)
        return hook

    def synchronize(self):
        pass

    @contextmanager
    def skip_synchronize(self):
        raise AssertionError("Skipping synchronization is not supported when using Adasum optimizer.")

    def step(self, closure=None):
        loss = None
        if closure is not None:
            loss = closure()

        missing_p = self._requires_update - set(self._handles.keys())
        for p in missing_p:
            handle, ctx = self._allreduce_grad_async(p)
            self._handles[p] = (handle, ctx)

        for p, (handle, ctx) in self._handles.items():
            # This means step() is called before backward_passes_per_steps finished.
            # We do a synchoronous allreduce here.
            if not handle:
                handle, ctx = self._allreduce_grad_async(p)
                self._handles[p] = (handle, ctx)
            delta = synchronize(handle)
            delta = self._compression.decompress(delta, ctx)
            start = self._starting_models[p]
            start.data.add_(delta.data)
            p.data.copy_(start)
            self._allreduce_delay[p] = self.backward_passes_per_step
        self._handles.clear()
        return loss

    def zero_grad(self):
        if self._handles:
            raise AssertionError("optimizer.zero_grad() was called after loss.backward() "
                                 "but before optimizer.step() or optimizer.synchronize(). "
                                 "This is prohibited as it can cause a race condition.")
        return super(self.__class__, self).zero_grad()


def DistributedOptimizer(optimizer, named_parameters=None,
                         compression=Compression.none,
                         backward_passes_per_step=1,
                         op=Average,
                         gradient_predivide_factor=1.0,
                         num_groups=0):
    """
    An optimizer that wraps another torch.optim.Optimizer, using an allreduce to
    combine gradient values before applying gradients to model weights.

    Allreduce operations are executed after each gradient is computed by ``loss.backward()``
    in parallel with each other. The ``step()`` method ensures that all allreduce operations are
    finished before applying gradients to the model.

    DistributedOptimizer exposes the ``synchronize()`` method, which forces allreduce operations
    to finish before continuing the execution. It's useful in conjunction with gradient
    clipping, or other operations that modify gradients in place before ``step()`` is executed.
    Make sure to use ``optimizer.skip_synchronize()`` if you're calling ``synchronize()``
    in your code.

    Example of gradient clipping:

    .. code-block:: python

        output = model(data)
        loss = F.nll_loss(output, target)
        loss.backward()
        optimizer.synchronize()
        torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip)
        with optimizer.skip_synchronize():
            optimizer.step()

    Arguments:
        optimizer: Optimizer to use for computing gradients and applying updates.
        named_parameters: A mapping between parameter names and values. Used for naming of
                          allreduce operations. Typically just ``model.named_parameters()``.
        compression: Compression algorithm used during allreduce to reduce the amount
                     of data sent during the each parameter update step.  Defaults to
                     not using compression.
        backward_passes_per_step: Number of expected backward passes to perform
                                  before calling step()/synchronize(). This
                                  allows accumulating gradients over multiple
                                  mini-batches before reducing and applying them.
        op: The reduction operation to use when combining gradients across different ranks.
        gradient_predivide_factor: If op == Average, gradient_predivide_factor splits the averaging
                                   before and after the sum. Gradients are scaled by
                                   1.0 / gradient_predivide_factor before the sum and
                                   gradient_predivide_factor / size after the sum.
        num_groups: Number of groups to assign gradient allreduce ops to for explicit
                    grouping. Defaults to no explicit groups.
    """
    # We dynamically create a new class that inherits from the optimizer that was passed in.
    # The goal is to override the `step()` method with an allreduce implementation.
    if gradient_predivide_factor != 1.0:
        if rocm_built():
            raise ValueError('gradient_predivide_factor not supported yet with ROCm')
        if op != Average:
            raise ValueError('gradient_predivide_factor not supported with op != Average')

    if op != Adasum or size() == 1:
        cls = type(optimizer.__class__.__name__, (optimizer.__class__,),
                   dict(_DistributedOptimizer.__dict__))
        return cls(optimizer.param_groups, named_parameters, compression, backward_passes_per_step, op,
                   gradient_predivide_factor, num_groups)
    else:
        cls = type(optimizer.__class__.__name__, (optimizer.__class__,),
                   dict(_DistributedAdasumOptimizer.__dict__))
        return cls(optimizer.param_groups, named_parameters, compression, backward_passes_per_step)

    def pickle(obj, buffer):
        cloudpickle.dump(obj,buffer)