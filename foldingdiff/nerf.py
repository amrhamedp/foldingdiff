"""
NERF!
Note that this was designed with compatibility with biotite, NOT biopython!
These two packages use different conventions for where NaNs are placed in dihedrals

References:
https://benjamin-computer.medium.com/protein-loops-in-tensorflow-a-i-bio-part-2-f1d802ef8300
https://www.biotite-python.org/examples/gallery/structure/peptide_assembly.html
"""
import os
from functools import cached_property
from typing import *

import numpy as np
import torch

N_CA_LENGTH = 1.46  # Check, approxiamtely right
CA_C_LENGTH = 1.54  # Check, approximately right
C_N_LENGTH = 1.34  # Check, approximately right

# Taken from initial coords from 1CRN, which is a THR
N_INIT = np.array([17.047, 14.099, 3.625])
CA_INIT = np.array([16.967, 12.784, 4.338])
C_INIT = np.array([15.685, 12.755, 5.133])


class NERFBuilder:
    """
    Builder for NERF
    """

    def __init__(
        self,
        phi_dihedrals: np.ndarray,
        psi_dihedrals: np.ndarray,
        omega_dihedrals: np.ndarray,
        bond_len_n_ca: Union[float, np.ndarray] = N_CA_LENGTH,
        bond_len_ca_c: Union[float, np.ndarray] = CA_C_LENGTH,
        bond_len_c_n: Union[float, np.ndarray] = C_N_LENGTH,  # 0C:1N distance
        bond_angle_n_ca: Union[float, np.ndarray] = 121 / 180 * np.pi,
        bond_angle_ca_c: Union[float, np.ndarray] = 109 / 180 * np.pi,  # aka tau
        bond_angle_c_n: Union[float, np.ndarray] = 115 / 180 * np.pi,
        init_coords: np.ndarray = [N_INIT, CA_INIT, C_INIT],
    ) -> None:
        self.use_torch = False
        if any(
            [
                isinstance(v, torch.Tensor)
                for v in [phi_dihedrals, psi_dihedrals, omega_dihedrals]
            ]
        ):
            self.use_torch = True

        self.phi = phi_dihedrals.squeeze()
        self.psi = psi_dihedrals.squeeze()
        self.omega = omega_dihedrals.squeeze()

        # We start with coordinates for N --> CA --> C so the next atom we add
        # is the next N. Therefore, the first angle we need is the C --> N bond
        self.bond_lengths = {
            ("C", "N"): bond_len_c_n,
            ("N", "CA"): bond_len_n_ca,
            ("CA", "C"): bond_len_ca_c,
        }
        self.bond_angles = {
            ("C", "N"): bond_angle_c_n,
            ("N", "CA"): bond_angle_n_ca,
            ("CA", "C"): bond_angle_ca_c,
        }
        self.init_coords = [c.squeeze() for c in init_coords]
        assert (
            len(self.init_coords) == 3
        ), f"Requires 3 initial coords for N-Ca-C but got {len(self.init_coords)}"
        assert all(
            [c.size == 3 for c in self.init_coords]
        ), "Initial coords should be 3-dimensional"

    @cached_property
    def cartesian_coords(self) -> Union[np.ndarray, torch.Tensor]:
        """Build out the molecule"""
        retval = self.init_coords.copy()
        if self.use_torch:
            retval = [torch.tensor(x, requires_grad=True) for x in retval]

        # The first value of phi at the N terminus is not defined
        # The last value of psi and omega at the C terminus are not defined
        for i, (phi, psi, omega) in enumerate(
            zip(self.phi[1:], self.psi[:-1], self.omega[:-1])
        ):
            # Procedure for placing N-CA-C
            # Place the next N atom, which requires the C-N bond length/angle, and the psi dihedral
            # Place the alpha carbon, which requires the N-CA bond length/angle, and the omega dihedral
            # Place the carbon, which requires the the CA-C bond length/angle, and the phi dihedral
            for bond, dih in zip(self.bond_lengths.keys(), [psi, omega, phi]):
                coords = place_dihedral(
                    retval[-3],
                    retval[-2],
                    retval[-1],
                    bond_angle=self._get_bond_angle(bond, i),
                    bond_length=self._get_bond_length(bond, i),
                    torsion_angle=dih,
                    use_torch=self.use_torch,
                )
                retval.append(coords)

        if self.use_torch:
            return torch.stack(retval)
        return np.array(retval)

    @cached_property
    def centered_cartesian_coords(self) -> Union[np.ndarray, torch.Tensor]:
        """Returns the centered coords"""
        means = self.cartesian_coords.mean(axis=0)
        return self.cartesian_coords - means

    def _get_bond_length(self, bond: Tuple[str, str], idx: int):
        """Get the ith bond distance"""
        v = self.bond_lengths[bond]
        if isinstance(v, float):
            return v
        return v[idx]

    def _get_bond_angle(self, bond: Tuple[str, str], idx: int):
        """Get the ith bond angle"""
        v = self.bond_angles[bond]
        if isinstance(v, float):
            return v
        return v[idx]


def place_dihedral(
    a: np.ndarray,
    b: np.ndarray,
    c: np.ndarray,
    bond_angle: float,
    bond_length: float,
    torsion_angle: float,
    use_torch: bool = False,
) -> Union[np.ndarray, torch.Tensor]:
    """
    Place the point d such that the bond angle, length, and torsion angle are satisfied
    with the series a, b, c, d.
    """
    assert a.ndim == b.ndim == c.ndim == 1

    if not use_torch:
        unit_vec = lambda x: x / np.linalg.norm(x)
        cross = np.cross
        stack = np.stack
    else:
        ensure_tensor = (
            lambda x: torch.tensor(x, requires_grad=False)
            if not isinstance(x, torch.Tensor)
            else x
        )
        a, b, c, bond_angle, bond_length, torsion_angle = [
            ensure_tensor(x) for x in (a, b, c, bond_angle, bond_length, torsion_angle)
        ]
        unit_vec = lambda x: x / torch.linalg.norm(x)
        cross = torch.cross
        stack = torch.stack

    ab = b - a
    bc = unit_vec(c - b)
    n = unit_vec(cross(ab, bc))
    nbc = cross(n, bc)
    m = stack([bc, nbc, n]).T

    if not use_torch:
        d = np.array(
            [
                -bond_length * np.cos(bond_angle),
                bond_length * np.cos(torsion_angle) * np.sin(bond_angle),
                bond_length * np.sin(torsion_angle) * np.sin(bond_angle),
            ]
        )
        d = m.dot(d)
    else:
        d = torch.vstack(
            [
                -bond_length * torch.cos(bond_angle),
                bond_length * torch.cos(torsion_angle) * torch.sin(bond_angle),
                bond_length * torch.sin(torsion_angle) * torch.sin(bond_angle),
            ]
        ).type(m.dtype)
        d = torch.mm(m, d).squeeze()
    # d = m.dot(d)
    return d + c


def main():
    """On the fly testing"""
    import biotite.structure as struc
    from biotite.structure.io.pdb import PDBFile

    source = PDBFile.read(
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "data/1CRN.pdb")
    )
    source_struct = source.get_structure()
    # print(source_struct[0])
    phi, psi, omega = [torch.tensor(x) for x in struc.dihedral_backbone(source_struct)]

    builder = NERFBuilder(phi, psi, omega)
    print(builder.cartesian_coords)
    print(builder.cartesian_coords.shape)


if __name__ == "__main__":
    main()
