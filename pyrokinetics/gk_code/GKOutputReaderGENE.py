import numpy as np
import xarray as xr
import f90nml
import logging
import struct
import csv
import re
import h5py
from typing import Tuple, Dict, Any, Optional
from pathlib import Path

from .GKOutputReader import GKOutputReader
from .GKInputGENE import GKInputGENE
from ..constants import pi
from ..typing import PathLike


class GKOutputReaderGENE(GKOutputReader):
    fields = ["phi", "apar", "bpar"]

    @staticmethod
    def _get_gene_files(filename: PathLike) -> Dict[str, Path]:
        """
        Given a directory name, looks for the files filename/parameters_0000,
        filename/field_0000 and filename/nrg_0000.
        If instead given any of the files parameters_####, field_#### or nrg_####,
        looks up the rest of the files in the same directory.
        """
        filename = Path(filename)
        prefixes = ["parameters", "field", "nrg", "omega"]
        if filename.is_dir():
            # If given a dir name, looks for dir/parameters_0000
            dirname = filename
            dat_matches = np.all(
                [Path(filename / f"{p}.dat").is_file() for p in prefixes]
            )
            if dat_matches:
                suffix = "dat"
                delimiter = "."
            else:
                suffix = "0000"
                delimiter = "_"
        else:
            # If given a file, searches for all similar GENE files in that file's dir
            dirname = filename.parent
            # Ensure provided file is a GENE file (fr"..." means raw format str)
            matches = [re.search(rf"^{p}_\d{{4}}$", filename.name) for p in prefixes]
            if not np.any(matches):
                raise RuntimeError(
                    f"GKOutputReaderGENE: The provided file {filename} is not a GENE "
                    "output file."
                )
            suffix = filename.name.split("_")[1]
            delimiter = "_"

        # Get all files in the same dir
        files = {
            prefix: dirname / f"{prefix}{delimiter}{suffix}"
            for prefix in prefixes
            if (dirname / f"{prefix}{delimiter}{suffix}").exists()
        }

        if not files:
            raise RuntimeError(
                "GKOutputReaderGENE: Could not find GENE output files in the "
                f"directory '{dirname}'."
            )
        if "parameters" not in files:
            raise RuntimeError(
                "GKOutputReaderGENE: Could not find GENE output file 'parameters_"
                f"{suffix}' when provided with the file/directory '{filename}'."
            )
        # If binary field file absent, adds .h5 field file,
        # if present, to 'files'
        if "field" not in files:
            if (dirname / f"field{delimiter}{suffix}.h5").exists():
                files.update({"field": dirname / f"field{delimiter}{suffix}.h5"})
        return files

    def verify(self, filename: PathLike):
        self._get_gene_files(filename)

    @staticmethod
    def infer_path_from_input_file(filename: PathLike) -> Path:
        """
        Given path to input file, guess at the path for associated output files.
        """
        # If the input file is of the form name_####, get the numbered part and
        # search for 'parameters_####' in the run directory. If not, simply return
        # the directory.
        filename = Path(filename)
        num_part_regex = re.compile(r"(\d{4})")
        num_part_match = num_part_regex.search(filename.name)

        if num_part_match is None:
            return Path(filename).parent
        else:
            return Path(filename).parent / f"parameters_{num_part_match[0]}"
        pass

    @classmethod
    def _get_raw_data(
        cls, filename: PathLike
    ) -> Tuple[Dict[str, Any], GKInputGENE, str]:
        files = cls._get_gene_files(filename)
        # Read parameters_#### as GKInputGENE and into plain string
        with open(files["parameters"], "r") as f:
            input_str = f.read()
        gk_input = GKInputGENE()
        gk_input.read_str(input_str)
        # Defer processing field and flux data until their respective functions
        # Simply return files in place of raw data
        return files, gk_input, input_str

    @classmethod
    def _init_dataset(
        cls, raw_data: Dict[str, Any], gk_input: GKInputGENE
    ) -> xr.Dataset:
        """
        Sets coords and attrs of a Pyrokinetics dataset from a GENE parameters file.

        Args:
            raw_data (Dict[str,Any]): Dict containing GENE output. Ignored.
            gk_input (GKInputGENE): Processed GENE input file.

        Returns:
            xr.Dataset: Dataset with coords and attrs set, but not data_vars
        """
        nml = gk_input.data

        ntime = (
            nml["info"]["steps"][0]
            // (gk_input.downsize * nml["in_out"]["istep_field"])
            + 1
        )
        # The last time step is not always written, but depends on
        # whatever condition is met first between simtimelim and timelim
        species = gk_input.get_local_species().names
        with open(raw_data["nrg"], "r") as f:
            lasttime = float(f.readlines()[-(len(species) + 1)])
        if lasttime == nml["general"]["simtimelim"]:
            ntime = ntime + 1

        delta_t = nml["info"]["step_time"][0]
        time = np.linspace(0, delta_t * (ntime - 1), ntime)

        nfield = nml["info"]["n_fields"]
        field = cls.fields[:nfield]

        nky = nml["box"]["nky0"]
        nkx = nml["box"]["nx0"]
        ntheta = nml["box"]["nz0"]
        theta = np.linspace(-pi, pi, ntheta, endpoint=False)

        nenergy = nml["box"]["nv0"]
        energy = np.linspace(-1, 1, nenergy)

        npitch = nml["box"]["nw0"]
        pitch = np.linspace(-1, 1, npitch)

        moment = ["particle", "energy", "momentum"]

        if gk_input.is_linear():
            # Set up ballooning angle
            single_theta_loop = theta
            single_ntheta_loop = ntheta

            ntheta = ntheta * (nkx - 1)
            theta = np.empty(ntheta)
            start = 0
            for i in range(nkx - 1):
                pi_segment = i - nkx // 2 + 1
                theta[start : start + single_ntheta_loop] = (
                    single_theta_loop + pi_segment * 2 * pi
                )
                start += single_ntheta_loop

            ky = [nml["box"]["kymin"]]
            kx = [0.0]
            nkx = 1
            # TODO should we not also set nky=1?

        else:
            kymin = nml["box"]["kymin"]
            ky = np.linspace(0, kymin * (nky - 1), nky)
            lx = nml["box"]["lx"]
            dkx = 2 * np.pi / lx
            kx = np.empty(nkx)
            for i in range(nkx):
                if i < (nkx / 2 + 1):
                    kx[i] = i * dkx
                else:
                    kx[i] = (i - nkx) * dkx

        # Store grid data as xarray DataSet
        return xr.Dataset(
            coords={
                "time": time,
                "kx": kx,
                "ky": ky,
                "theta": theta,
                "energy": energy,
                "pitch": pitch,
                "moment": moment,
                "field": field,
                "species": species,
            },
            attrs={
                "ntime": ntime,
                "nkx": nkx,
                "nky": nky,
                "ntheta": ntheta,
                "nenergy": nenergy,
                "npitch": npitch,
                "nmoment": len(moment),
                "nfield": nfield,
                "nspecies": len(species),
                "linear": gk_input.is_linear(),
            },
        )

    @staticmethod
    def _set_fields(
        data: xr.Dataset, raw_data: Dict[str, Any], gk_input: GKInputGENE
    ) -> xr.Dataset:
        """
        Sets 3D fields over time.
        The field coordinates should be (field, theta, kx, ky, time)
        """
        coords = ["field", "theta", "kx", "ky", "time"]

        if "field" not in raw_data:
            return data

        # The following is slightly edited from GKCodeGENE:
        # =================================================

        # Time data stored as binary (int, double, int)
        time = []
        time_data_fmt = "=idi"
        time_data_size = struct.calcsize(time_data_fmt)

        int_size = 4
        complex_size = 16

        downsize = gk_input.downsize

        nx = gk_input.data["box"]["nx0"]
        nz = gk_input.data["box"]["nz0"]

        field_size = nx * nz * data.nky * complex_size

        sliced_field = np.empty(
            (data.nfield, nx, data.nky, nz, data.ntime), dtype=complex
        )
        fields = np.empty(
            (data.nfield, data.nkx, data.nky, data.ntheta, data.ntime), dtype=complex
        )
        # Read binary file if present
        if ".h5" not in str(raw_data["field"]):
            with open(raw_data["field"], "rb") as file:
                for i_time in range(data.ntime):
                    # Read in time data (stored as int, double int)
                    time_value = float(
                        struct.unpack(time_data_fmt, file.read(time_data_size))[1]
                    )
                    time.append(time_value)
                    for i_field in range(data.nfield):
                        file.seek(int_size, 1)
                        binary_field = file.read(field_size)
                        raw_field = np.frombuffer(binary_field, dtype=np.complex128)
                        sliced_field[i_field, :, :, :, i_time] = raw_field.reshape(
                            (nx, data.nky, nz),
                            order="F",
                        )
                        file.seek(int_size, 1)
                    if i_time < data.ntime - 1:
                        file.seek(
                            (downsize - 1)
                            * (
                                time_data_size
                                + data.nfield * (2 * int_size + field_size)
                            ),
                            1,
                        )

        # Read .h5 file if binary file absent
        else:
            h5_field_subgroup_names = ["phi", "A_par", "B_par"]
            fields = np.empty(
                (data.nfield, data.nkx, data.nky, data.ntheta, data.ntime),
                dtype=complex,
            )
            with h5py.File(raw_data["field"], "r") as file:
                # Read in time data
                time.extend(list(file.get("field/time")))
                for i_field in range(data.nfield):
                    h5_subgroup = "field/" + h5_field_subgroup_names[i_field] + "/"
                    h5_dataset_names = list(file[h5_subgroup].keys())
                    for i_time in range(data.ntime):
                        h5_dataset = h5_subgroup + h5_dataset_names[i_time]
                        raw_field = np.array(file.get(h5_dataset))
                        raw_field = np.array(
                            raw_field["real"] + raw_field["imaginary"] * 1j,
                            dtype="complex128",
                        )
                        sliced_field[i_field, :, :, :, i_time] = np.swapaxes(
                            raw_field, 0, 2
                        )

        # Match pyro convention for ion/electron direction
        sliced_field = np.conjugate(sliced_field)

        if not data.linear:
            nl_shape = (data.nfield, data.nkx, data.nky, data.ntheta, data.ntime)
            fields = sliced_field.reshape(nl_shape, order="F")

        # Convert from kx to ballooning space
        else:
            try:
                n0_global = gk_input.data["box"]["n0_global"]
                q0 = gk_input.data["geometry"]["q0"]
                phase_fac = -np.exp(-2 * np.pi * 1j * n0_global * q0)
            except KeyError:
                phase_fac = -1
            i_ball = 0

            for i_conn in range(-int(nx / 2) + 1, int((nx - 1) / 2) + 1):
                fields[:, 0, :, i_ball : i_ball + nz, :] = (
                    sliced_field[:, i_conn, :, :, :] * (phase_fac) ** i_conn
                )
                i_ball += nz

        # =================================================

        # Overwrite 'time' coordinate as determined in _init_dataset
        data["time"] = time

        # Original method coords: (field, kx, ky, theta, time)
        # New coords: (field, theta, kx, ky, time)
        fields = fields.transpose(0, 3, 1, 2, 4)

        data["fields"] = (coords, fields)

        return data

    @staticmethod
    def _set_fluxes(
        data: xr.Dataset, raw_data: Dict[str, Any], gk_input: GKInputGENE
    ) -> xr.Dataset:
        """
        Set flux data over time.
        The flux coordinates should  be (species, moment, field, ky, time)
        """

        # ky data not available in the nrg file so no ky coords here
        coords = ("species", "moment", "field", "time")
        fluxes = np.empty([data.dims[coord] for coord in coords])

        if "nrg" not in raw_data:
            logging.warning("Flux data not found, setting all fluxes to zero")
            fluxes[:, :, :, :] = 0
            data["fluxes"] = (coords, fluxes)
            return data

        nml = f90nml.read(raw_data["parameters"])
        flux_istep = nml["in_out"]["istep_nrg"]
        field_istep = nml["in_out"]["istep_field"]

        ntime_flux = nml["info"]["steps"][0] // flux_istep
        if nml["info"]["steps"][0] % flux_istep > 0:
            ntime_flux = ntime_flux + 1

        downsize = gk_input.downsize

        if flux_istep < field_istep:
            time_skip = int(field_istep * downsize / flux_istep) - 1
        else:
            time_skip = downsize - 1

        with open(raw_data["nrg"], "r") as csv_file:
            nrg_data = csv.reader(csv_file, delimiter=" ", skipinitialspace=True)

            if data.nfield == 3:
                logging.warning(
                    "GENE combines Apar and Bpar fluxes, setting Bpar fluxes to zero"
                )
                fluxes[:, :, 2, :] = 0.0
                field_size = 2
            else:
                field_size = data.nfield

            for i_time in range(data.ntime):
                time = next(nrg_data)  # noqa

                for i_species in range(data.nspecies):
                    nrg_line = np.array(next(nrg_data), dtype=float)

                    # Particle
                    fluxes[i_species, 0, :field_size, i_time] = nrg_line[
                        4 : 4 + field_size,
                    ]

                    # Energy
                    fluxes[i_species, 1, :field_size, i_time] = nrg_line[
                        6 : 6 + field_size,
                    ]

                    # Momentum
                    fluxes[i_species, 2, :field_size, i_time] = nrg_line[
                        8 : 8 + field_size,
                    ]

                # Skip time/data values in field print out is less
                if i_time < data.ntime - 2:
                    for skip_t in range(time_skip):
                        for skip_s in range(data.nspecies + 1):
                            next(nrg_data)

        data["fluxes"] = (coords, fluxes)
        return data

    @staticmethod
    def _set_eigenvalues(
        data: xr.Dataset, raw_data: Optional[Any] = None, gk_input: Optional[Any] = None
    ) -> xr.Dataset:
        if "fields" in data:
            return GKOutputReader._set_eigenvalues(data, raw_data, gk_input)

        logging.warn(
            "'fields' not set in data, falling back to reading 'omega' -- 'eigenvalues' will not be set!"
        )

        kys = []
        frequencies = []
        growth_rates = []

        with open(raw_data["omega"], "r") as csv_file:
            omega_data = csv.reader(csv_file, delimiter=" ", skipinitialspace=True)
            for line in omega_data:
                ky, growth_rate, frequency = line
                kys.append(float(ky))
                frequencies.append(float(frequency))
                growth_rates.append(float(growth_rate))

        last_timestep = [data.time.isel(time=-1)]
        coords = {"time": last_timestep, "ky": kys, "kx": [0.0]}

        data["mode_frequency"] = xr.DataArray(
            np.array(frequencies, ndmin=3), coords=coords
        )
        data["growth_rate"] = xr.DataArray(
            np.array(growth_rates, ndmin=3), coords=coords
        )

        return data
