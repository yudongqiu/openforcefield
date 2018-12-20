#!/usr/bin/env python

# =============================================================================================
# MODULE DOCSTRING
# =============================================================================================

"""
Property estimator client side API.

Authors
-------
* Simon Boothroyd <simon.boothroyd@choderalab.org>

"""


# =============================================================================================
# GLOBAL IMPORTS
# =============================================================================================

import logging
import json
import struct

from simtk import unit

from pydantic import BaseModel
from typing import Dict, List

from tornado import gen
from tornado.ioloop import IOLoop
from tornado.iostream import StreamClosedError
from tornado.tcpclient import TCPClient

from openforcefield.utils.serialization import serialize_quantity

from openforcefield.properties import CalculationFidelity, PhysicalProperty
from openforcefield.properties.estimator import CalculationSchema

from openforcefield.typing.engines.smirnoff import ForceField

int_struct = struct.Struct("<i")

unpack_int = int_struct.unpack
pack_int = int_struct.pack


# =============================================================================================
# Registration Decorators
# =============================================================================================

def register_estimable_property():
    """A decorator which registers a property as being estimable
    by the property estimator.

    Notes
    -----
    The property must implement a static get_calculation_template method
    which returns the calculation schema to follow.
    """

    def decorator(cls):

        if cls.__name__ in PropertyEstimator.registered_properties:
            raise ValueError('The {} property is already registered.'.format(cls.__name__))

        PropertyEstimator.registered_properties[cls.__name__] = cls
        return cls

    return decorator


# =============================================================================================
# Property Estimator
# =============================================================================================

class PropertyEstimatorOptions(BaseModel):
    """Represents additional options that can be passed to the
    property estimator backend."""

    allowed_fidelity: CalculationFidelity = CalculationFidelity.SurrogateModel | \
                                            CalculationFidelity.Reweighting | \
                                            CalculationFidelity.DirectSimulation

    calculation_schemas: Dict[str, CalculationSchema] = {}


class PropertyEstimatorDataModel(BaseModel):

    properties: Dict[str, List[PhysicalProperty]] = {}
    options: PropertyEstimatorOptions = None

    parameter_set: Dict[int, str] = None

    class Config:

        # A dirty hack to allow simtk.unit.Quantities...
        # TODO: Should really investigate QCElemental as an alternative.
        arbitrary_types_allowed = True

        json_encoders = {
            unit.Quantity: lambda v: serialize_quantity(v),
        }


class PropertyEstimator(object):
    """
    The object responsible for requesting a set of properties
    be calculated by the low-level property calculation backend,
    and for analysing the performance of the parameters.
    """

    registered_properties = {}

    def __init__(self, server_address='localhost', port=8000):
        """Constructs a new PropertyEstimator object.

        Parameters
        ----------
        server_address : str
            The address of the calculation server.
        """

        self._server_address = server_address

        if server_address is None:

            raise ValueError('The address of the server which will run'
                             'these calculations must be given.')

        self._port = port
        self._tcp_client = TCPClient()

    def compute_properties(self, data_set, parameter_set, additional_options=None):
        """
        Submit the property and parameter set for calculation.

        Parameters
        ----------
        data_set : PropertyDataSet
            The set of properties to attempt to compute.
        parameter_set : ForceField
            The OpenFF parameter set to use for the calculations.
        additional_options : PropertyEstimatorOptions, optional
            A set of additional calculation options.

        Returns
        -------
        list of str:
            A list unique ids which can be used to retrieve the submitted calculations
            when they have finished running.
        """

        if data_set is None or parameter_set is None:

            raise ValueError('Both a data set and parameter set must be '
                             'present to compute physical properties.')

        if additional_options is None:
            additional_options = PropertyEstimatorOptions()

        for substance_tag in data_set.properties:

            for physical_property in data_set.properties[substance_tag]:

                type_name = type(physical_property).__name__

                if type_name not in PropertyEstimator.registered_properties:

                    raise ValueError('The property estimator does not support {} '
                                     'properties.'.format(type_name))

                if type_name in additional_options.calculation_schemas:
                    continue

                additional_options.calculation_schemas[type_name] = \
                    PropertyEstimator.registered_properties[type_name]().get_default_calculation_schema()

        submission_packet = PropertyEstimatorDataModel()

        submission_packet.properties = data_set.properties
        submission_packet.parameter_set = parameter_set.__getstate__()
        submission_packet.options = additional_options

        submission_json = submission_packet.json()

        # For now just do a blocking submit to the server.
        ticket_ids = IOLoop.current().run_sync(lambda: self._send_calculations_to_server(submission_json))

        return ticket_ids

    @gen.coroutine
    def _send_calculations_to_server(self, submission_json):

        ticket_ids = None

        try:

            logging.info("Attempting Connection to {}:{}".format(self._server_address, self._port))

            stream = yield self._tcp_client.connect(self._server_address, self._port)

            logging.info("Connected to {}:{}".format(self._server_address, self._port))

            stream.set_nodelay(True)

            encoded_json = submission_json.encode()
            length = pack_int(len(encoded_json))
            yield stream.write(length + encoded_json)

            logging.info("Sent calculations to {}:{}".format(self._server_address, self._port))

            header = yield stream.read_bytes(4)

            # Convert from network order to int.
            length = unpack_int(header)[0]

            encoded_json = yield stream.read_bytes(length)
            ticket_ids = json.loads(encoded_json.decode())

            logging.info('Received job ids from server: {}'.format(ticket_ids))

        except StreamClosedError as e:
            logging.info("Error connecting to {}:{} : {}".format(self._server_address, self._port, e))

        raise gen.Return(ticket_ids)

    @staticmethod
    def _store_properties_in_hierarchy(original_set):
        """Refactor a property list into a hierarchy of substance->state->type.

        Parameters
        ----------
        original_set : dict(str, list(PhysicalProperty))
            The set of properties to refactor.
        """
        property_hierarchy = {}

        for substance_tag in original_set:

            for calculated_property in original_set[substance_tag]:

                if substance_tag not in property_hierarchy:
                    property_hierarchy[substance_tag] = {}

                state_tag = hash(calculated_property.thermodynamic_state)

                if state_tag not in property_hierarchy[substance_tag]:
                    property_hierarchy[substance_tag][state_tag] = {}

                if calculated_property.type not in property_hierarchy[substance_tag][state_tag]:
                    property_hierarchy[substance_tag][state_tag][calculated_property.type] = {}

                property_hierarchy[substance_tag][state_tag][calculated_property.type] = calculated_property

        return property_hierarchy

    @staticmethod
    def produce_calculation_report(measured_data_set, calculated_data_set):
        """
        Produce a report detailing how well a measured and calculated data
        set match.

        Parameters
        ----------
        measured_data_set : PhysicalPropertyDataSet
            The set of measured properties to compare against.
        calculated_data_set : CalculatedPropertySet
            The set of calculated properties to analyse.
        """
        measured_properties = PropertyEstimator._store_properties_in_hierarchy(
            measured_data_set.properties)

        calculated_properties = PropertyEstimator._store_properties_in_hierarchy(
            calculated_data_set.properties)

        for substance in calculated_properties:

            for state in calculated_properties[substance]:

                if len(calculated_properties[substance][state]) <= 0:
                    continue

                state_string = next(iter(calculated_properties[substance][state].values())).thermodynamic_state

                logging.info('PROPERTIES FOR ' + substance + ' AT ' + str(state_string))

                for property_type in calculated_properties[substance][state]:

                    measured_property = measured_properties[substance][state][property_type]
                    calculated_property = calculated_properties[substance][state][property_type]

                    logging.info('Property: ' + str(property_type) +
                                 ' Measured: ' + str(measured_property.value) +
                                 '(' + str(measured_property.uncertainty) + ')' +
                                 ' Calculated: ' + str(calculated_property.value) +
                                 '(' + str(calculated_property.uncertainty) + ')')

        return