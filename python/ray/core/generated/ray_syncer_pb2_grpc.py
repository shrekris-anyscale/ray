# Generated by the gRPC Python protocol compiler plugin. DO NOT EDIT!
"""Client and server classes corresponding to protobuf-defined services."""
import grpc

from . import ray_syncer_pb2 as src_dot_ray_dot_protobuf_dot_ray__syncer__pb2


class RaySyncerStub(object):
    """Missing associated documentation comment in .proto file."""

    def __init__(self, channel):
        """Constructor.

        Args:
            channel: A grpc.Channel.
        """
        self.StartSync = channel.stream_stream(
                '/ray.rpc.syncer.RaySyncer/StartSync',
                request_serializer=src_dot_ray_dot_protobuf_dot_ray__syncer__pb2.RaySyncMessage.SerializeToString,
                response_deserializer=src_dot_ray_dot_protobuf_dot_ray__syncer__pb2.RaySyncMessage.FromString,
                )


class RaySyncerServicer(object):
    """Missing associated documentation comment in .proto file."""

    def StartSync(self, request_iterator, context):
        """Missing associated documentation comment in .proto file."""
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details('Method not implemented!')
        raise NotImplementedError('Method not implemented!')


def add_RaySyncerServicer_to_server(servicer, server):
    rpc_method_handlers = {
            'StartSync': grpc.stream_stream_rpc_method_handler(
                    servicer.StartSync,
                    request_deserializer=src_dot_ray_dot_protobuf_dot_ray__syncer__pb2.RaySyncMessage.FromString,
                    response_serializer=src_dot_ray_dot_protobuf_dot_ray__syncer__pb2.RaySyncMessage.SerializeToString,
            ),
    }
    generic_handler = grpc.method_handlers_generic_handler(
            'ray.rpc.syncer.RaySyncer', rpc_method_handlers)
    server.add_generic_rpc_handlers((generic_handler,))


 # This class is part of an EXPERIMENTAL API.
class RaySyncer(object):
    """Missing associated documentation comment in .proto file."""

    @staticmethod
    def StartSync(request_iterator,
            target,
            options=(),
            channel_credentials=None,
            call_credentials=None,
            insecure=False,
            compression=None,
            wait_for_ready=None,
            timeout=None,
            metadata=None):
        return grpc.experimental.stream_stream(request_iterator, target, '/ray.rpc.syncer.RaySyncer/StartSync',
            src_dot_ray_dot_protobuf_dot_ray__syncer__pb2.RaySyncMessage.SerializeToString,
            src_dot_ray_dot_protobuf_dot_ray__syncer__pb2.RaySyncMessage.FromString,
            options, channel_credentials,
            insecure, call_credentials, compression, wait_for_ready, timeout, metadata)