# Owner(s): ["module: dynamo"]
import importlib
import unittest

import torch

import torch._dynamo
import torch._dynamo.test_case
from torch._dynamo.optimizations import backends
from torch._dynamo.testing import same


def has_onnxruntime():
    try:
        importlib.import_module("onnxruntime")
        return True
    except ImportError:
        return False


def has_ipex():
    try:
        importlib.import_module("intel_extension_for_pytorch")
        return True
    except ImportError:
        return False


def has_functorch():
    try:
        importlib.import_module("functorch")
        return True
    except ImportError:
        return False


class Seq(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.layers = torch.nn.Sequential(
            torch.nn.Linear(10, 10),
            torch.nn.ReLU(),
            torch.nn.Linear(10, 10),
            torch.nn.Sigmoid(),
        )

    def forward(self, x):
        return self.layers(x)


class Conv_Bn_Relu(torch.nn.Module):
    def __init__(self, in_channels, out_channels, **kwargs):
        super(Conv_Bn_Relu, self).__init__()
        self.conv = torch.nn.Conv2d(in_channels, out_channels, bias=False, **kwargs)
        self.bn = torch.nn.BatchNorm2d(out_channels, eps=0.001)
        self.relu = torch.nn.ReLU()

    def forward(self, x):
        return self.relu(self.bn(self.conv(x)))


class TestOptimizations(torch._dynamo.test_case.TestCase):
    def test_example_inputs(self):
        def fn(a, bc, d):
            b, c = bc
            return a / d - b / c

        def compiler_fn(graph, example_inputs):
            nonlocal r1
            r1 = graph(*example_inputs)[0]
            return graph.forward

        a = torch.empty(2).fill_(1)
        b = torch.empty(2).fill_(2)
        c = torch.empty(2).fill_(3)
        d = 4
        r1 = None
        r2 = fn(a, (b, c), d)
        opt_fn = torch._dynamo.optimize_assert(compiler_fn)(fn)
        r3 = opt_fn(a, (b, c), d)

        self.assertIsNotNone(r1)
        self.assertEqual(r1.size(), r2.size())
        self.assertEqual(r1.stride(), r2.stride())
        self.assertEqual(r1.dtype, r2.dtype)

        self.assertEqual(r1.size(), r3.size())
        self.assertEqual(r1.stride(), r3.stride())
        self.assertEqual(r1.dtype, r3.dtype)

    def test_example_inputs_runtime_use(self):
        def fn(a, bc, d):
            b, c = bc
            return a / d - b / c

        def compiler_fn(graph, example_inputs):
            def fwd(*args):
                nonlocal r1
                r = graph.forward(*args)
                r1 = r[0]
                return r

            return fwd

        a = torch.empty(2).fill_(1)
        b = torch.empty(2).fill_(2)
        c = torch.empty(2).fill_(3)
        d = 4
        r1 = None
        r2 = fn(a, (b, c), d)
        opt_fn = torch._dynamo.optimize_assert(compiler_fn)(fn)
        r3 = opt_fn(a, (b, c), d)

        self.assertIsNotNone(r1)
        self.assertTrue(same(r1, r2))
        self.assertTrue(same(r1, r3))

    @unittest.skipIf(not has_ipex(), "requires ipex")
    def test_ipex_fp32(self):
        model = Conv_Bn_Relu(3, 32, kernel_size=3, stride=1)
        model = model.to(memory_format=torch.channels_last)
        model = model.eval()
        input = torch.randn(8, 3, 64, 64).contiguous(memory_format=torch.channels_last)
        r1 = model(input)
        opt_model = torch._dynamo.optimize(backends.ipex_fp32)(model)
        with torch.no_grad():
            r2 = opt_model(input)
        self.assertTrue(same(r1, r2))
        self.assertEqual(r2.dtype, torch.float32)

    @unittest.skipIf(not has_ipex(), "requires ipex")
    def test_ipex_bf16(self):
        model = Conv_Bn_Relu(3, 32, kernel_size=3, stride=1)
        model = model.to(memory_format=torch.channels_last)
        model = model.eval()
        input = torch.randn(8, 3, 64, 64).contiguous(memory_format=torch.channels_last)
        r1 = model(input)
        opt_model = torch._dynamo.optimize(backends.ipex_bf16)(model)
        with torch.no_grad(), torch.cpu.amp.autocast():
            r2 = opt_model(input)
        self.assertTrue(same(r1, r2.float(), tol=0.1))
        self.assertEqual(r2.dtype, torch.bfloat16)

    def _check_backend_works(self, backend):
        model = Conv_Bn_Relu(3, 32, kernel_size=3, stride=1).eval()
        input = torch.randn(8, 3, 64, 64)
        r1 = model(input)
        r2 = torch.compile(model, backend=backend)(input)
        self.assertTrue(same(r1, r2.float(), tol=0.01))

    def test_eager(self):
        self._check_backend_works("eager")

    def test_torchscript(self):
        self._check_backend_works("ts")


class NormalizeIRTests(torch._dynamo.test_case.TestCase):
    @unittest.skipIf(not has_functorch(), "requires functorch")
    def test_inplace_normalize(self):
        def fn(a, b):
            x = torch.cos(a)
            x += b
            return torch.sin(x)

        a = torch.randn(10)
        b = torch.randn(10).to(torch.float64)

        ref = fn(a, b)

        optimized_fn = torch._dynamo.optimize("aot_eager")(fn)
        res = optimized_fn(a, b)
        self.assertTrue(same(ref, res))


if __name__ == "__main__":
    from torch._dynamo.test_case import run_tests

    run_tests()
