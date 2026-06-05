# ========================================================================== #
#                                                                            #
#    KVMD - The main PiKVM daemon.                                           #
#                                                                            #
#    Copyright (C) 2018-2024  Maxim Devaev <mdevaev@gmail.com>               #
#                                                                            #
#    This program is free software: you can redistribute it and/or modify    #
#    it under the terms of the GNU General Public License as published by    #
#    the Free Software Foundation, either version 3 of the License, or       #
#    (at your option) any later version.                                     #
#                                                                            #
#    This program is distributed in the hope that it will be useful,         #
#    but WITHOUT ANY WARRANTY; without even the implied warranty of          #
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the           #
#    GNU General Public License for more details.                            #
#                                                                            #
#    You should have received a copy of the GNU General Public License       #
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.  #
#                                                                            #
# ========================================================================== #


import os
from urllib.parse import urlparse
from pathlib import PurePosixPath

from aiohttp.web import Request
from aiohttp.web import Response

from .....logging import get_logger

from .....htserver import HttpError
from .....htserver import exposed_http
from .....htserver import make_json_response

from .....plugins.msd import BaseMsd

from .....validators.basic import valid_bool
from .....validators.kvm import valid_msd_image_name
from .....validators.basic import valid_int_f0


# =====
class RedfishMsdApi:
    # https://pubs.lenovo.com/tsm/get_virtual_media_collection
    # https://developer.avermedia.com/oob/fw-1.0.3.1/user-guide/13-virtualmedia

    def __init__(self, msd: BaseMsd) -> None:
        self.__msd = msd

    # =====

    @exposed_http("GET", "/redfish/v1/Managers")
    async def __managers_handler(self, _: Request) -> Response:
        return make_json_response({
            "@odata.id":   "/redfish/v1/Managers",
            "@odata.type": "#ManagerCollection.ManagerCollection",
            "Name":        "Manager Collection",
            "Members": [{"@odata.id": "/redfish/v1/Managers/BMC"}],
            "Members@odata.count": 1,
        }, wrap_result=False)

    @exposed_http("GET", "/redfish/v1/Managers/BMC")
    async def __managers_bmc_handler(self, _: Request) -> Response:
        return make_json_response({
            "@odata.id":    "/redfish/v1/Managers/BMC",
            "@odata.type":  "#Manager.v1_15_0.Manager",
            "Id":           "BMC",
            "Name":         "PiKVM Manager",
            "Description":  "PiKVM Baseboard Management Controller",
            "ManagerType":  "BMC",
            "VirtualMedia": {"@odata.id": "/redfish/v1/Managers/BMC/VirtualMedia"},
        }, wrap_result=False)

    @exposed_http("GET", "/redfish/v1/Managers/BMC/VirtualMedia")
    async def __managers_bmc_vm_handler(self, _: Request) -> Response:
        return make_json_response({
            "@odata.id":   "/redfish/v1/Managers/BMC/VirtualMedia",
            "@odata.type": "#VirtualMediaCollection.VirtualMediaCollection",
            "Name":        "Virtual Media Collection",
            "Members": [{"@odata.id": "/redfish/v1/Managers/BMC/VirtualMedia/MSD"}],
            "Members@odata.count": 1,
        }, wrap_result=False)

    # =====

    @exposed_http("GET", "/redfish/v1/Managers/BMC/VirtualMedia/MSD")
    async def __msd_handler(self, _: Request) -> Response:
        state = (await self.__msd.get_state())

        drive: (dict | None) = None
        path: (str | None) = None
        if state["online"]:
            drive = state["drive"]
            path = (drive and drive["image"] and drive["image"]["name"])  # type: ignore

        return make_json_response({
            "@odata.id":      "/redfish/v1/Managers/BMC/VirtualMedia/MSD",
            "@odata.type":    "#VirtualMedia.v1_4_0.VirtualMedia",
            "Id":             "MSD",
            "Name":           "Virtual CD/DVD/Flash Drive",
            "Description":    "PiKVM Virtual CD/DVD/Flash Drive",
            "MediaTypes":     ["USBStick", "CD", "DVD"],
            "Image":          path,
            "ImageName":      (path and os.path.basename(path)),
            "ConnectedVia":   (drive and ("Oem" if drive["image"] else "NotConnected")),
            "Inserted":       (drive and drive["connected"]),
            "WriteProtected": (drive and drive["rw"]),
            "Oem": {
                "PiKVM": {
                    "@odata.context": "/redfish/v1/$metadata#PiKVMVirtualMedia.PiKVMVirtualMedia",
                    "@odata.type":    "#PiKVMVirtualMedia.v1_0_0.PiKVMVirtualMedia",
                    "MsdEnabled":     state["enabled"],
                    "MsdOnline":      state["online"],
                    "MsdBusy":        state["busy"],
                    "DriveOptical":   (drive and drive["cdrom"]),
                },
            },
            "Actions": {
                "#VirtualMedia.InsertMedia": {
                    "target": "/redfish/v1/Managers/BMC/VirtualMedia/MSD/Actions/VirtualMedia.InsertMedia",
                    "Image@Redfish.AllowableValues": ["URI"],
                },
                "#VirtualMedia.EjectMedia": {
                    "target": "/redfish/v1/Managers/BMC/VirtualMedia/MSD/Actions/VirtualMedia.EjectMedia",
                },
            },
        }, wrap_result=False)

    @exposed_http("POST", "/redfish/v1/Managers/BMC/VirtualMedia/MSD/Actions/VirtualMedia.InsertMedia")
    async def __msd_insert_handler(self, req: Request) -> Response:
        try:
            params = await req.json()
        except Exception:
            raise HttpError("Invalid body", 400)

        image = valid_msd_image_name(params.get("Image"))
        logger = get_logger(0)

        logger.info(f"Image: {image}")

        if is_http_url(image):
            logger.info("Download image")
            await download_and_write_image(
                name,
                True,
            )
            name = PurePosixPath(urlparse(image).path).name
            logger.info(f"Downloaded. Image name: {name}")
        else:
            name = image

        cdrom = name.lower().startswith(".iso")
        connect = valid_bool(params.get("Inserted", True))
        rw = (not valid_bool(params.get("WriteProtected", True)))

        state = await self.__msd.get_state()
        if state.get("drive", {}).get("connected"):
            await self.__msd.set_connected(False)
            await self.__msd.set_params(name="")

        await self.__msd.set_params(name=name, cdrom=cdrom, rw=rw)
        if connect:
            await self.__msd.set_connected(True)
        return Response(body=None, status=204)

    @exposed_http("POST", "/redfish/v1/Managers/BMC/VirtualMedia/MSD/Actions/VirtualMedia.EjectMedia")
    async def __msd_eject_handler(self, _: Request) -> Response:
        await self.__msd.set_connected(False)
        await self.__msd.set_params(name="")
        return Response(body=None, status=204)


def is_http_url(s: str) -> bool:
    try:
        result = urlparse(s)
        return result.scheme in ("http", "https") and bool(result.netloc)
    except Exception:
        return False




async def download_and_write_image(url: str, secure: bool, timeout: float = 60.0):

    # async def stream_write_info() -> None:
    #     assert resp is not None
    #     await stream_json(resp, self.__make_write_info(name, size, written))
    #
    # try:
    async with htclient.download(
        url=url,
        verify=(not insecure),
        timeout=timeout,
        read_timeout=(7 * 24 * 3600),
    ) as remote:

        name = str(req.query.get("image", "")).strip()
        if len(name) == 0:
            name = htclient.get_filename(remote)
        name = valid_msd_image_name(unsafe_prefix + name)

        size = valid_int_f0(remote.content_length)

        get_logger(0).info("Downloading image %r as %r to MSD ...", url, name)
        async with self.__msd.write_image(name, size, remove_incomplete) as writer:
            chunk_size = writer.get_chunk_size()
            # resp = await start_streaming(req, "application/x-ndjson")
            # await stream_write_info()
            # last_report_ts = 0
            async for chunk in remote.content.iter_chunked(chunk_size):
                written = await writer.write_chunk(chunk)
                # now = int(time.monotonic())
                # if last_report_ts + 1 < now:
                #     await stream_write_info()
                #     last_report_ts = now

        # await stream_write_info()
        # return resp

    # except Exception as ex:
    #     if resp is not None:
    #         await stream_write_info()
    #         await stream_json_exception(resp, ex)
    #     elif isinstance(ex, aiohttp.ClientError):
    #         return make_json_exception(ex, 400)
    #     raise
