"""Steps for convergence."""
from uuid import uuid4

import attr
from attr.validators import instance_of

from characteristic import Attribute, attributes

from effect import Constant, Effect, Func, catch

from pyrsistent import PMap, PSet, freeze, pset, thaw

import six

from toolz.dicttoolz import dissoc, get_in

from twisted.python.constants import NamedConstant

from zope.interface import Interface, implementer

from otter.cloud_client import (
    CreateServerConfigurationError,
    CreateServerOverQuoteError,
    check_stack,
    create_server,
    create_stack,
    delete_stack,
    has_code,
    rcv3,
    service_request,
    set_nova_metadata_item,
    update_stack)
from otter.cloud_client.clb import (
    CLBNodeLimitError,
    CLBNotFoundError,
    NoSuchCLBNodeError,
    add_clb_nodes,
    change_clb_node,
    remove_clb_nodes)
from otter.constants import ServiceType
from otter.convergence.model import ErrorReason, HeatStack, StepResult
from otter.util.fp import set_in
from otter.util.hashkey import generate_server_name
from otter.util.http import APIError, append_segments
from otter.util.retry import (
    exponential_backoff_interval,
    retry_effect,
    retry_times)


class IStep(Interface):
    """
    An :obj:`IStep` is a step that may be performed within the context of a
    converge operation.
    """

    def as_effect():
        """
        Return an Effect which performs this step.

        :return: A two-tuple of a :obj:`StepResult` and a list of
        :obj:`ErrorReason`s.
        """


def set_server_name(server_config_args, name_suffix):
    """
    Append the given name_suffix to the name of the server in the server
    config.

    :param server_config_args: The server configuration args.
    :param name_suffix: the suffix to append to the server name. If no name was
        specified, it will be used as the name.
    """
    name = server_config_args['server'].get('name')
    if name is not None:
        name = '{0}-{1}'.format(name, name_suffix)
    else:
        name = name_suffix
    return set_in(server_config_args, ('server', 'name'), name)


def append_stack_uuid(stack_config, uuid):
    """
    Append the given uuid to the `stack_name` value in `stack_config`.
    """
    name_key = ('stack_name',)
    name = get_in(name_key, stack_config)
    return set_in(stack_config, name_key, name + '_%s' % uuid)


def _ignore_errors(*ignored_err_types):
    """
    Return an error-handler function that returns None if the exception matches
    any of the given error types.
    """
    def handler(exc_info):
        if isinstance(exc_info[1], ignored_err_types):
            return None
        six.reraise(*exc_info)
    return handler


def _failure_reporter(*terminal_err_types):
    """
    Return a callable that takes an error tuple which interprets the error
    tuple.

    If the error is an APIError with status code 4xx, or one of the provided
    ``terminal_err_types``, then the callable returns a tuple of::

        (StepResult.FAILURE, [ErrorReason.Exception(exc_tuple)])

    else it returns a tuple of::

        (StepResult.RETRY, [ErrorReason.Exception(exc_tuple)])
    """
    def reporter(exc_tuple):
        err_type, error, traceback = exc_tuple

        terminal_error = (
            any(issubclass(err_type, etype)
                for etype in terminal_err_types) or
            err_type == APIError and 400 <= error.code < 500)

        if terminal_error:
            return StepResult.FAILURE, [ErrorReason.Exception(exc_tuple)]
        return StepResult.RETRY, [ErrorReason.Exception(exc_tuple)]

    return reporter


def _success_reporter(success_reason):
    """
    Return a callable that takes a result and returns a::

        (StepResult.RETRY, [ErrorReason.String(success_reason)])
    """
    def reporter(_):
        return StepResult.RETRY, [ErrorReason.String(success_reason)]
    return reporter


@implementer(IStep)
@attributes([Attribute('server_config', instance_of=PMap)])
class CreateServer(object):
    """
    A server must be created.

    :ivar pmap server_config: Nova launch configuration.
    """

    def as_effect(self):
        """Produce a :obj:`Effect` to create a server."""
        eff = Effect(Func(generate_server_name))

        def got_name(random_name):
            server_config = set_server_name(self.server_config, random_name)
            return create_server(thaw(server_config))

        return eff.on(got_name).on(
            success=_success_reporter('waiting for server to become active'),
            error=_failure_reporter(CreateServerConfigurationError,
                                    CreateServerOverQuoteError))


class UnexpectedServerStatus(Exception):
    """
    An exception to be raised when a server is found in an unexpected state.
    """
    def __init__(self, server_id, status, expected_status):
        super(UnexpectedServerStatus, self).__init__(
            'Expected {server_id} to have {expected_status}, '
            'has {status}'.format(server_id=server_id,
                                  status=status,
                                  expected_status=expected_status)
        )
        self.server_id = server_id
        self.status = status
        self.expected_status = expected_status


def delete_and_verify(server_id):
    """
    Check the status of the server to see if it's actually been deleted.
    Succeeds only if it has been either deleted (404) or acknowledged by Nova
    to be deleted (task_state = "deleted").

    Note that ``task_state`` is in the server details key
    ``OS-EXT-STS:task_state``, which is supported by Openstack but available
    only when looking at the extended status of a server.
    """

    def check_task_state((resp, server_blob)):
        if resp.code == 404:
            return
        server_details = server_blob['server']
        is_deleting = server_details.get("OS-EXT-STS:task_state", "")
        if is_deleting.strip().lower() != "deleting":
            raise UnexpectedServerStatus(server_id, is_deleting, "deleting")

    def verify((_type, error, traceback)):
        if error.code != 204:
            raise _type, error, traceback
        ver_eff = service_request(
            ServiceType.CLOUD_SERVERS, 'GET',
            append_segments('servers', server_id),
            success_pred=has_code(200, 404))
        return ver_eff.on(check_task_state)

    return service_request(
        ServiceType.CLOUD_SERVERS, 'DELETE',
        append_segments('servers', server_id),
        success_pred=has_code(404)).on(error=catch(APIError, verify))


@implementer(IStep)
@attributes([Attribute('server_id', instance_of=basestring)])
class DeleteServer(object):
    """
    A server must be deleted.

    :ivar str server_id: a Nova server ID.
    """

    def as_effect(self):
        """Produce a :obj:`Effect` to delete a server."""

        eff = retry_effect(
            delete_and_verify(self.server_id), can_retry=retry_times(3),
            next_interval=exponential_backoff_interval(2))

        def report_success(result):
            return StepResult.RETRY, [
                ErrorReason.String(
                    'must re-gather after deletion in order to update the '
                    'active cache')]

        return eff.on(success=report_success)


@implementer(IStep)
@attributes([Attribute('server_id', instance_of=basestring),
             Attribute('key', instance_of=basestring),
             Attribute('value', instance_of=basestring)])
class SetMetadataItemOnServer(object):
    """
    A metadata key/value item must be set on a server.

    :ivar str server_id: a Nova server ID.
    :ivar str key: The metadata key to set (<=256 characters)
    :ivar str value: The value to assign to the metadata key (<=256 characters)

    Succeed unconditionally on 200 (success).  Everything else can probably
    be retried, since nothing is a catastrophic group failure.
    """
    def as_effect(self):
        """Produce a :obj:`Effect` to set a metadata item on a server"""
        eff = set_nova_metadata_item(
            server_id=self.server_id, key=self.key, value=self.value)

        return eff.on(success=lambda _: (StepResult.SUCCESS, []))


@implementer(IStep)
@attributes([Attribute('lb_id', instance_of=basestring),
             Attribute('address_configs', instance_of=PSet)])
class AddNodesToCLB(object):
    """
    Multiple nodes must be added to a load balancer.

    Note: This is not correctly documented in the load balancer documentation -
    it is documented as "Add Node" (singular), but the examples show multiple
    nodes being added.

    :ivar str lb_id: The cloud load balancer ID to add nodes to.
    :ivar iterable address_configs: A collection of two-tuples of address and
        :obj:`CLBDescription`.

    Retry if successful (to re-gather to update the active cache) or if there
    was a non-terminal failure (if there were duplicate nodes, if the CLB is
    in PENDING_UDPATE, or if the CLB rate-limited the request).  These can
    all be fixed in the next convergence cycle.

    Fail otherwise.
    """
    def as_effect(self):
        """Produce a :obj:`Effect` to add nodes to CLB"""
        eff = add_clb_nodes(
            self.lb_id,
            [{'address': address, 'port': lbc.port,
              'condition': lbc.condition.name, 'weight': lbc.weight,
              'type': lbc.type.name}
             for address, lbc in self.address_configs])

        return eff.on(
            success=_success_reporter(
                'must re-gather after adding to CLB in order to update '
                'the active cache'),
            error=_failure_reporter(CLBNotFoundError, CLBNodeLimitError))


@implementer(IStep)
@attributes([Attribute('lb_id', instance_of=basestring),
             Attribute('node_ids', instance_of=PSet)])
class RemoveNodesFromCLB(object):
    """
    One or more IPs must be removed from a load balancer.

    :ivar str lb_id: The cloud load balancer ID to remove nodes from.
    :ivar iterable node_ids: A collection of node IDs to remove from the CLB.
    """

    def as_effect(self):
        """Produce a :obj:`Effect` to remove a load balancer node."""
        eff = remove_clb_nodes(self.lb_id, self.node_ids)
        # Since we're deleting a node, we'll ignore any errors which indicate
        # that the node doesn't exist.
        return eff.on(
            error=_ignore_errors(CLBNotFoundError, NoSuchCLBNodeError)
        ).on(
            success=lambda r: (StepResult.SUCCESS, []),
            error=_failure_reporter())


@implementer(IStep)
@attributes([Attribute('lb_id', instance_of=basestring),
             Attribute('node_id', instance_of=basestring),
             Attribute('condition', instance_of=NamedConstant),
             Attribute('weight', instance_of=int),
             Attribute('type', instance_of=NamedConstant)])
class ChangeCLBNode(object):
    """
    An existing port mapping on a load balancer must have its condition,
    weight, or type modified.
    """
    def as_effect(self):
        """Produce a :obj:`Effect` to modify a load balancer node."""
        eff = change_clb_node(self.lb_id, self.node_id, weight=self.weight,
                              condition=self.condition.name,
                              _type=self.type.name)
        return eff.on(
            success=lambda _: (StepResult.RETRY, [ErrorReason.String(
                'must re-gather after CLB change in order to update the '
                'active cache')]),
            error=_failure_reporter(CLBNotFoundError, NoSuchCLBNodeError))


@implementer(IStep)
@attributes([Attribute('lb_node_pairs', instance_of=PSet)])
class BulkAddToRCv3(object):
    """
    Some connections must be made between some combination of servers
    and RackConnect v3.0 load balancers.

    Each connection is independently specified.

    See http://docs.rcv3.apiary.io/#post-%2Fv3%2F{tenant_id}
    %2Fload_balancer_pools%2Fnodes.

    :param list lb_node_pairs: A list of ``lb_id, node_id`` tuples of
        connections to be made.
    """
    def as_effect(self):
        """
        Produce a :obj:`Effect` to add some nodes to some RCv3 load
        balancers.
        """
        eff = rcv3.bulk_add(self.lb_node_pairs)
        return eff.on(
            success=lambda _: (StepResult.RETRY, [ErrorReason.String(
                'must re-gather after LB add in order to update the '
                'active cache')]),
            error=catch(rcv3.BulkErrors, _handle_bulk_add_errors))


def _handle_bulk_add_errors(exc_tuple):
    error = exc_tuple[1]
    failures = []
    retries = []
    for excp in error.errors:
        if isinstance(excp, rcv3.ServerUnprocessableError):
            retries.append(ErrorReason.String(excp.message))
        else:
            failures.append(ErrorReason.String(excp.message))
    if failures:
        return StepResult.FAILURE, failures
    else:
        return StepResult.RETRY, retries


@implementer(IStep)
@attributes([Attribute('lb_node_pairs', instance_of=PSet)])
class BulkRemoveFromRCv3(object):
    """
    Some connections must be removed between some combination of nodes
    and RackConnect v3.0 load balancers.

    See http://docs.rcv3.apiary.io/#delete-%2Fv3%2F{tenant_id}
    %2Fload_balancer_pools%2Fnodes.

    :param list lb_node_pairs: A list of ``lb_id, node_id`` tuples of
        connections to be removed.
    """
    def as_effect(self):
        """
        Produce a :obj:`Effect` to remove some nodes from some RCv3 load
        balancers.
        """
        eff = rcv3.bulk_delete(self.lb_node_pairs)
        return eff.on(
            success=lambda _: (StepResult.RETRY, [ErrorReason.String(
                'must re-gather after RCv3 LB change in order to update the '
                'active cache')]),
            error=_failure_reporter(rcv3.BulkErrors))


@implementer(IStep)
@attr.s(init=False)
class ConvergeLater(object):
    """
    Converge later in some time
    """
    reasons = attr.ib()
    limited = attr.ib()

    def __init__(self, reasons, limited=False):
        self.reasons = pset(reasons)
        self.limited = limited

    def as_effect(self):
        """
        Return an effect that always results in retry
        """
        result = StepResult.LIMITED_RETRY if self.limited else StepResult.RETRY
        return Effect(Constant((result, list(self.reasons))))


@implementer(IStep)
@attr.s(init=False)
class FailConvergence(object):
    """Convergence cannot continue, put the group into an error state."""
    reasons = attr.ib()

    def __init__(self, reasons):
        self.reasons = freeze(reasons)

    def as_effect(self):
        """Return an effect that always results in failure."""
        return Effect(Constant((StepResult.FAILURE, list(self.reasons))))


# ----- Cloud Orchestration Steps -----


@implementer(IStep)
@attr.s
class CreateStack(object):
    """
    A stack must be created.

    :ivar pmap stack_config: Heat launch configuration.

    """
    stack_config = attr.ib(validator=instance_of(PMap))

    def as_effect(self):
        """Produce a :obj:`Effect` to create a stack."""
        eff = Effect(Func(uuid4))

        def got_uuid(uuid):
            stack_config = append_stack_uuid(self.stack_config, uuid)
            return create_stack(thaw(stack_config)).on(
                _success_reporter('Waiting for stack to create'))

        return eff.on(got_uuid)


@implementer(IStep)
@attr.s
class CheckStack(object):
    """
    A stack's resources must be checked to see if any are in an error state.
    Returns RETRY.
    """
    stack = attr.ib(validator=instance_of(HeatStack))

    def as_effect(self):
        """Produce a :obj:`Effect` to check a stack's resources."""
        eff = check_stack(stack_name=self.stack.name, stack_id=self.stack.id)
        return eff.on(_success_reporter('Waiting for stack check to complete'))


@implementer(IStep)
@attr.s
class UpdateStack(object):
    """
    A stack must be updated. Returns RETRY unless retry=False is passed upon
    instantiation.
    """
    stack = attr.ib(validator=instance_of(HeatStack))
    stack_config = attr.ib(validator=instance_of(PMap))
    retry = attr.ib(default=True)

    def as_effect(self):
        """Produce an :obj:`Effect` to update a stack."""
        stack_config = dissoc(thaw(self.stack_config), 'stack_name')
        eff = update_stack(stack_name=self.stack.name, stack_id=self.stack.id,
                           stack_args=stack_config)

        def report_success(result):
            retry_msg = 'Waiting for stack to update'
            return ((StepResult.RETRY, [ErrorReason.String(retry_msg)])
                    if self.retry else (StepResult.SUCCESS, []))

        return eff.on(success=report_success)


@implementer(IStep)
@attr.s
class DeleteStack(object):
    """
    A stack must be deleted.
    Returns RETRY.
    """
    stack = attr.ib(validator=instance_of(HeatStack))

    def as_effect(self):
        """Produce a :obj:`Effect` to delete a stack."""
        eff = delete_stack(stack_name=self.stack.name, stack_id=self.stack.id)

        def report_success(result):
            return StepResult.RETRY, [
                ErrorReason.String('Waiting for stack to delete')]

        return eff.on(success=report_success)
