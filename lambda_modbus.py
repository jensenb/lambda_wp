import argparse
import asyncio
import logging
import logging.config
import pathlib
from struct import pack, unpack
from time import time
from typing import Dict, List, NoReturn

import influxdb_client
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync
from influxdb_client.client.write_api import SYNCHRONOUS, Point
from pymodbus.client import ModbusTcpClient
from ruamel.yaml import YAML

HEAT_PUMP_MODE = {
    0: "STBY",
    1: "CH",
    2: "DHW",
    3: "CC",
    4: "CIRCULATE",
    5: "DEFROST",
    6: "OFF",
    7: "FROST",
    8: "STBY-FROST",
    10: "SUMMER",
    11: "HOLIDAY",
    12: "ERROR",
    13: "WARNING",
    14: "INFO-MESSAGE",
    15: "TIME-BLOCK",
    16: "RELEASE-BLOCK",
    17: "MINTEMP-BLOCK",
    18: "FIRMWARE-DOWNLOAD",
}

HEAT_PUMP_STATE = {
    0: "INIT",
    1: "REFERENCE",
    2: "RESTART-BLOCK",
    3: "READY",
    4: "START PUMPS",
    5: "START COMPRESSOR",
    6: "PRE-REGULATION",
    7: "REGULATION",
    9: "COOLING",
    10: "DEFROSTING",
    20: "STOPPING",
    30: "FAULT-LOCK",
    31: "ALARM-BLOCK",
    40: "ERROR-RESET",
}

BOILER_MODE = {
    0: "STBY",
    1: "DHW",
    2: "LEGIO",
    3: "SUMMER",
    4: "FROST",
    5: "HOLIDAY",
    6: "PRIO-STOP",
    7: "ERROR",
    8: "OFF",
    9: "PROMPT-DHW",
    10: "TRAILING-STOP",
    11: "TEMP-LOCK",
    12: "STBY-FROST",
}

BUFFER_MODE = {
    0: "STBY",
    1: "HEATING",
    2: "COOLING",
    3: "SUMMER",
    4: "FROST",
    5: "HOLIDAY",
    6: "PRIO-STOP",
    7: "ERROR",
    8: "OFF",
    9: "STBY-FROST",
}

SOLAR_MODE = {0: "STBY", 1: "HEATING", 2: "ERROR", 3: "OFF"}

HEATING_CIRCUIT_STATE = {
    0: "HEATING",
    1: "ECO",
    2: "COOLING",
    3: "FLOORDRY",
    4: "FROST",
    5: "MAX-TEMP",
    6: "ERROR",
    7: "SERVICE",
    8: "HOLIDAY",
    9: "CH-SUMMER",
    10: "CC-WINTER",
    11: "PRIO-STOP",
    12: "OFF",
    13: "RELEASE-OFF",
    14: "TIME-OFF",
    15: "STBY",
    16: "STBY-HEATING",
    17: "STBY-ECO",
    18: "STBY-COOLING",
    19: "STBY-FROST",
    20: "STBY-FLOORDRY",
}

HEATING_CIRCUIT_MODE = {
    0: "OFF",
    1: "MANUAL",
    2: "AUTOMATIK",
    3: "AUTO-HEATING",
    4: "AUTO-COOLING",
    5: "FROST",
    6: "SUMMER",
    7: "FLOOR-DRY",
}

HEAT_PUMP_REQUEST_TYPE = {
    0: "NO REQUEST",
    1: "FLOW PUMP CIRCULATION",
    2: "CENTRAL HEATING",
    3: "CENTRAL COOLING",
    4: "DOMESTIC HOT WATER",
}

ENERGY_MANAGER_STATE = {
    0: "OFF",
    1: "AUTOMATIK",
    2: "MANUAL",
    3: "ERROR",
    4: "OFFLINE",
}


def fixed_point_to_float_10(x):
    return float(x) / 10


def fixed_point_to_float_100(x):
    return float(x) / 100


def as_int(x):
    return int(x)


def heat_pump_mode_to_str(x):
    return HEAT_PUMP_MODE[x]


def heat_pump_state_to_str(x):
    return HEAT_PUMP_STATE[x]


def boiler_mode_to_str(x):
    return BOILER_MODE[x]


def buffer_mode_to_str(x):
    return BUFFER_MODE[x]


def solar_mode_to_str(x):
    return SOLAR_MODE[x]


def heating_circuit_state_to_str(x):
    return HEATING_CIRCUIT_STATE[x]


def heating_circuit_mode_to_str(x):
    return HEATING_CIRCUIT_MODE[x]


def heat_pump_request_type_to_str(x):
    return HEAT_PUMP_REQUEST_TYPE[x]


def energy_manager_operating_state_to_str(x):
    return ENERGY_MANAGER_STATE[x]


def as_is(x):
    return x


class LambdaModbuxException(Exception):
    pass


class ConnectionManager:
    def __init__(
        self,
        lambda_host: str,
        lambda_port: int,
        influxdb_host: str,
        influxdb_port: int,
        influxdb_token: str,
        influxdb_org: str,
        influxdb_bucket: str,
    ):

        self.lambda_host = lambda_host
        self.lambda_port = lambda_port
        self.bucket = influxdb_bucket

        self.influxdb_host = influxdb_host
        self.influxdb_port = influxdb_port
        self.influxdb_token = influxdb_token
        self.influxdb_org = influxdb_org

    async def _init(self):
        self.lambda_client = ModbusTcpClient(host=self.lambda_host, port=self.lambda_port)

        self.influxdb_client = InfluxDBClientAsync(
            url=f"http://{self.influxdb_host}:{self.influxdb_port}", token=self.influxdb_token, org=self.influxdb_org
        )
        ready = await self.influxdb_client.ping()
        logging.debug(f"InfluxDB client ready: {ready}")
        self.write_api = self.influxdb_client.write_api()

    async def write_record(self, record: Point):

        success = await self.write_api.write(bucket=self.bucket, record=record)
        if success:
            logging.debug(f"Successfully wrote {record} to influxdb")
        else:
            logging.error(f"Error writing record {record} to influxdb")

    def read_lambda_values(self, register, count):
        logging.debug(f"Reading register {register} from {self.lambda_host}:{self.lambda_port}")
        res = self.lambda_client.read_holding_registers(register, count)
        if res.isError():
            logging.error(f"Error reading register {register} count {count} from Lambda via Modbus.")
            raise LambdaModbuxException(f"Error reading register {register} count {count} from Lambda via Modbus.")

        logging.debug(f"Retrieved {len(res.registers)} values")

        return res.registers

    async def read_lambda_vals(
        self,
        register: int,
        measurement_group: str,
        names: List,
        funcs: List,
        tags: List,
        count: int,
        format: str,
        interval=5.0,
        cache_values: bool = True,
        denoise_values: bool = True,
        cache_timeout: float = 300.0,
    ) -> NoReturn:

        cached_vals = [[0.0, time()] for _ in names]

        while True:
            res_enc = self.read_lambda_values(register=register, count=count)

            # Modbus is big endian, pack the results in a byte string to be reinterpreted according
            # to the actual register type
            pack_format = ">" + "".join(["H" for _ in range(count)])
            res_bytes = pack(pack_format, *res_enc)
            vals = unpack(format, res_bytes)

            p = Point(measurement_group)
            for i in range(len(names)):
                logging.debug(f"Processing {names[i]}")

                # Skip values marked as None in the config
                if names[i] == "None":
                    continue

                val = funcs[i](vals[i])
                t_now = time()

                # state info we write as tags on every measurement for selecting orther measurements
                # based upon the state value
                if tags[i]:
                    p = p.tag(names[i], val)
                    logging.debug(f"Writing tag {names[i]}")

                    # additionally we write the state also as field for easier visualization
                    p_state = Point(measurement_group)
                    if (
                        cache_values
                        and (val != cached_vals[i][0] or t_now - cached_vals[i][1] >= cache_timeout)
                        or not cache_values
                    ):

                        logging.info(f"Writing field {names[i]}: {val}")
                        p_state = p_state.field(names[i], val)
                        cached_vals[i][0] = val
                        cached_vals[i][1] = t_now
                        await self.write_record(p_state)

                # caching only for field measurements
                elif cache_values:

                    # We only denoise floating point values (from temp sensors primarily)
                    if denoise_values:
                        if abs(cached_vals[i][0] - val) > 0.11 or t_now - cached_vals[i][1] >= cache_timeout:
                            logging.info(f"Writing field {names[i]}: {val}")
                            p = p.field(names[i], val)
                            cached_vals[i][0] = val
                            cached_vals[i][1] = t_now
                        else:
                            logging.debug(f"Not writing field {names[i]}")

                    elif cached_vals[i][0] != val or t_now - cached_vals[i][1] >= cache_timeout:
                        logging.info(f"Writing field {names[i]}: {val}")
                        p = p.field(names[i], val)
                        cached_vals[i][0] = val
                        cached_vals[i][1] = t_now
                    else:
                        logging.debug(f"Not writing field {names[i]}")
                else:
                    logging.info(f"Writing field {names[i]}: {val}")
                    p = p.field(names[i], val)

            await self.write_record(p)
            await asyncio.sleep(interval)


async def create_connection_manager(
    lambda_host: str,
    lambda_port: int,
    influxdb_host: str,
    influxdb_port: int,
    influxdb_token: str,
    influxdb_org: str,
    influxdb_bucket: str,
) -> ConnectionManager:

    conn_man = ConnectionManager(
        lambda_host=lambda_host,
        lambda_port=lambda_port,
        influxdb_host=influxdb_host,
        influxdb_port=influxdb_port,
        influxdb_token=influxdb_token,
        influxdb_org=influxdb_org,
        influxdb_bucket=influxdb_bucket,
    )

    await conn_man._init()

    return conn_man


async def main():
    """
    Small command line processing function
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="Path to the configuration file", required=True)
    parser.add_argument(
        "--log-level",
        help="Log level used by the script",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    # setup logging
    logging.basicConfig(level=args.log_level, format="%(asctime)s [%(levelname)s] %(module)s:%(lineno)d - %(message)s")

    # Load the specified config file
    yaml = YAML()
    config = yaml.load(pathlib.Path(args.config))

    conn_man = await create_connection_manager(
        lambda_host=config["lambda"]["host"],
        lambda_port=config["lambda"]["port"],
        influxdb_host=config["influxdb"]["host"],
        influxdb_port=config["influxdb"]["port"],
        influxdb_token=config["influxdb"]["token"],
        influxdb_org=config["influxdb"]["org"],
        influxdb_bucket=config["influxdb"]["bucket"],
    )

    tasks = []
    for m_group in config["measurements"]:
        m_group["funcs"] = list(map(lambda x: globals()[x], m_group["funcs"]))

        t = asyncio.create_task(conn_man.read_lambda_vals(**m_group))
        tasks.append(t)
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
