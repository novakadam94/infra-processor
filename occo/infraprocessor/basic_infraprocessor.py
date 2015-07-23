#
# Copyright (C) 2014 MTA SZTAKI
#

""" Basic Infrastructure Processor for OCCO

.. moduleauthor:: Adam Visegradi <adam.visegradi@sztaki.mta.hu>
"""

__all__ = ['BasicInfraProcessor',
           'CreateInfrastructure', 'CreateNode', 'DropNode', 'DropInfrastructure']

import logging
import occo.util.factory as factory
import occo.infobroker as ib
from occo.infraprocessor.node_resolution.resolution import resolve_node
import uuid
import yaml
from occo.infraprocessor.infraprocessor import InfraProcessor, Command
from occo.infraprocessor.strategy import Strategy
from occo.exceptions.orchestration import NodeCreationError

log = logging.getLogger('occo.infraprocessor.basic')

###############
## IP Commands

class CreateInfrastructure(Command):
    """
    Implementation of infrastructure creation using a
    :ref:`service composer <servicecomposer>`.

    :param str infra_id: The identifier of the infrastructure instance.

    The ``infra_id`` is a unique identifier pre-generated by the
    :ref:`Compiler <compiler>`. The infrastructure will be instantiated with
    this identifier.
    """
    def __init__(self, infra_id):
        Command.__init__(self)
        self.infra_id = infra_id

    def perform(self, infraprocessor):
        return infraprocessor.servicecomposer.create_infrastructure(
            self.infra_id)

class CreateNode(Command):
    """
    Implementation of node creation using a
    :ref:`service composer <servicecomposer>` and a
    :ref:`cloud handler <cloudhandler>`.

    :param node: The description of the node to be created.
    :type node: :ref:`nodedescription`

    """
    def __init__(self, node_description):
        Command.__init__(self)
        self.node_description = node_description

    def perform(self, infraprocessor):
        """
        Start the node.

        This implementation is **incomplete**. We need to:

        .. todo:: Handle errors when creating the node (if necessary; it is
            possible that they are best handled in the InfraProcessor itself).

        .. warning:: Does the parallelized strategy propagate errors
            properly? Must verify!

        .. todo::
            Handle all known possible statuses

        .. todo:: We synchronize on the node becoming completely ready
            (started, configured). We need a **timeout** on this.

        """

        node_description = self.node_description
        instance_data = dict(
            node_id=str(uuid.uuid4()),
            user_id=node_description['user_id'],
            node_description=node_description,
        )

        try:
            self._perform_create(infraprocessor, instance_data)
        except KeyboardInterrupt:
            # A KeyboardInterrupt is considered an intentional cancellation,
            # thus it is not an error per se
            raise
        except Exception as ex:
            raise NodeCreationError(instance_data, ex)
        else:
            log.info("Node %s/%s/%s has started",
                     node_description['infra_id'],
                     node_description['name'],
                     instance_data['node_id'])
            return instance_data

    def _perform_create(self, infraprocessor, instance_data):
        """
        Core to :meth:`perform`. Used to avoid a level of nesting.
        """

        # Quick-access references
        node_id = instance_data['node_id']
        ib = infraprocessor.ib
        node_description = self.node_description

        log.debug('Performing CreateNode on node {\n%s}',
                  yaml.dump(node_description, default_flow_style=False))

        # Resolve all the information required to instantiate the node using
        # the abstract description and the UDS/infobroker
        resolved_node_def = resolve_node(ib, node_id, node_description)
        log.debug("Resolved node description:\n%s",
                  yaml.dump(resolved_node_def, default_flow_style=False))
        instance_data['resolved_node_definition'] = resolved_node_def
        instance_data['backend_id'] = resolved_node_def['backend_id']

        # Create the node based on the resolved information
        infraprocessor.servicecomposer.register_node(resolved_node_def)
        instance_id = infraprocessor.cloudhandler.create_node(resolved_node_def)
        instance_data['instance_id'] = instance_id

        import occo.infraprocessor.synchronization as synch

        infraprocessor.uds.register_started_node(
            node_description['infra_id'],
            node_description['name'],
            instance_data)

        log.info(
            "Node %s/%s/%s received address: %r (%s)",
            node_description['infra_id'],
            node_description['name'],
            node_id,
            ib.get('node.resource.address', instance_data),
            ib.get('node.resource.ip_address', instance_data))

        # TODO Add timeout
        synch.wait_for_node(instance_data, infraprocessor.poll_delay)

        return instance_data

class DropNode(Command):
    """
    Implementation of node deletion using a
    :ref:`service composer <servicecomposer>` and a
    :ref:`cloud handler <cloudhandler>`.

    :param instance_data: The description of the node instance to be deleted.
    :type instance_data: :ref:`instancedata`

    """
    def __init__(self, instance_data):
        Command.__init__(self)
        self.instance_data = instance_data
    def perform(self, infraprocessor):
        infraprocessor.cloudhandler.drop_node(self.instance_data)
        infraprocessor.servicecomposer.drop_node(self.instance_data)

class DropInfrastructure(Command):
    """
    Implementation of infrastructure deletion using a
    :ref:`service composer <servicecomposer>`.

    :param str infra_id: The identifier of the infrastructure instance.
    """
    def __init__(self, infra_id):
        Command.__init__(self)
        self.infra_id = infra_id

    def perform(self, infraprocessor):
        infraprocessor.servicecomposer.drop_infrastructure(self.infra_id)

####################
## IP implementation

@factory.register(InfraProcessor, 'basic')
class BasicInfraProcessor(InfraProcessor):
    """
    Implementation of :class:`InfraProcessor` using the primitives defined in
    this module.

    :param user_data_store: Database manipulation.
    :type user_data_store: :class:`~occo.infobroker.UDS`

    :param cloudhandler: Cloud access.
    :type cloudhandler: :class:`~occo.cloudhandler.cloudhandler.CloudHandler`

    :param servicecomposer: Service composer access.
    :type servicecomposer:
        :class:`~occo.servicecomposer.servicecomposer.ServiceComposer`

    :param process_strategy: Plug-in strategy for performing an independent
        batch of instructions.
    :type process_strategy: :class:`Strategy`

    :param int poll_delay: Node creation is synchronized on the node becoming
        completely operational. This condition has to be polled in
        :meth:`CreateNode.perform`. ``poll_delay`` is the number of seconds to
        wait between polls.
    """
    def __init__(self, user_data_store,
                 cloudhandler, servicecomposer,
                 process_strategy='sequential',
                 poll_delay=10,
                 **config):
        super(BasicInfraProcessor, self).__init__(
            process_strategy=process_strategy)
        self.__dict__.update(config)
        self.ib = ib.main_info_broker
        self.uds = user_data_store
        self.cloudhandler = cloudhandler
        self.servicecomposer = servicecomposer
        self.poll_delay = poll_delay

    def cri_create_infrastructure(self, infra_id):
        return CreateInfrastructure(infra_id)
    def cri_create_node(self, node_description):
        return CreateNode(node_description)
    def cri_drop_node(self, node_id):
        return DropNode(node_id)
    def cri_drop_infrastructure(self, infra_id):
        return DropInfrastructure(infra_id)
