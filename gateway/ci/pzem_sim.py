"""CI helper: a Modbus TCP simulator holding a PZEM-016 register frame (used to smoke-test the exe)."""
import asyncio
import sys

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext
from pymodbus.server import ModbusTcpServer


async def main(port: int, seconds: int):
    ir = [2301, 1234, 0, 2839, 0, 12345, 0, 600, 92, 0] + [0] * 10
    store = ModbusSlaveContext(ir=ModbusSequentialDataBlock(1, ir), hr=ModbusSequentialDataBlock(1, [0] * 20),
                               co=ModbusSequentialDataBlock(1, [False] * 20), di=ModbusSequentialDataBlock(1, [False] * 20))
    server = ModbusTcpServer(ModbusServerContext(slaves={1: store}, single=False), address=("127.0.0.1", port))
    task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(seconds)
    await server.shutdown()
    task.cancel()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), int(sys.argv[2])))
