"""
Code to convert from angles between residues to XYZ coordinates. 

Based on: 
https://github.com/biopython/biopython/blob/master/Bio/PDB/ic_rebuild.py
"""
import os
import logging
from typing import *

import numpy as np
import pandas as pd
import scipy.linalg

from Bio import PDB
from Bio.PDB import PICIO, ic_rebuild


def pdb_to_pic(pdb_file: str, pic_file: str):
    """
    Convert the PDB file to a PIC file
    """
    parser = PDB.PDBParser(QUIET=True)
    s = parser.get_structure("pdb", pdb_file)
    chains = [c for c in s.get_chains()]
    if len(chains) > 1:
        raise NotImplementedError
    chain = chains.pop()  # type Bio.PDB.Chain.Chain
    # print(chain.__dict__.keys())

    # Convert to relative angles
    # Calculate dihedrals, angles, bond lengths (internal coordinates) for Atom data
    # Generates atomArray through init_edra
    chain.atom_to_internal_coordinates()

    for res in chain.internal_coord.ordered_aa_ic_list:
        # Look at only analines because that's what we generate
        if res.residue.get_resname() != "ALA":
            continue
        # print("REF", res, type(res))
        # print(res.dihedra.keys())

    with open(pic_file, "w") as sink:
        PICIO.write_PIC(chain, sink)


def pic_to_pdb(pic_file: str, pdb_file: str):
    """
    Read int he PIC file and convert to a PDB file
    """
    with open(pic_file) as source:
        f = PICIO.read_PIC(source)
    f.internal_to_atom_coordinates()

    io = PDB.PDBIO()
    io.set_structure(f)
    io.save(pdb_file)


def canonical_distances_and_dihedrals(
    fname: str,
    distances=["0C:1N"],
    angles=["phi", "psi", "omega", "tau"],
    use_radians: bool = True,
) -> Optional[pd.DataFrame]:
    """
    Parse PDB from fname. Returns an array of distance and angles
    https://foldit.fandom.com/wiki/Backbone_angle - There are

    https://biopython.org/wiki/Reading_large_PDB_files
    """
    parser = PDB.PDBParser(QUIET=True)

    s = parser.get_structure("", fname)
    # s.atom_to_internal_coordinates()
    # s.internal_to_atom_coordinates()

    # If there are multiple chains then skip and return None
    chains = [c for c in s.get_chains()]
    if len(chains) > 1:
        logging.warning(f"{fname} has multiple chains, returning None")
        return None
    chain = chains.pop()
    chain.atom_to_internal_coordinates()

    residues = [r for r in chain.get_residues() if r.get_resname() not in ("HOH", "NA")]

    values = []
    # https://biopython.org/docs/dev/api/Bio.PDB.internal_coords.html#Bio.PDB.internal_coords.IC_Chain
    ic = chain.internal_coord  # Type IC_Chain
    if not ic_rebuild.structure_rebuild_test(chain)["pass"]:
        # https://biopython.org/docs/dev/api/Bio.PDB.ic_rebuild.html#Bio.PDB.ic_rebuild.structure_rebuild_test
        logging.warning(f"{fname} failed rebuild test, returning None")
        return None

    # Attributes
    # - dAtoms: homogeneous atom coordinates (4x4) of dihedra, second atom at origin
    # - hAtoms: homogeneous atom coordinates (3x4) of hedra, central atom at origin
    # - dihedra: Dihedra forming residues in this chain; indexed by 4-tuples of AtomKeys.
    # - ordered_aa_ic_list: IC_Residue objects in order of appearance in the chain.
    # https://biopython.org/docs/dev/api/Bio.PDB.internal_coords.html#Bio.PDB.internal_coords.IC_Residue
    for ric in ic.ordered_aa_ic_list:
        # https://biopython.org/docs/dev/api/Bio.PDB.internal_coords.html#Bio.PDB.internal_coords.IC_Residue.pick_angle
        this_dists = np.array([ric.get_length(d) for d in distances], dtype=np.float64)
        this_angles = np.array([ric.get_angle(a) for a in angles], dtype=np.float64)
        this_angles_nonnan = ~np.isnan(this_angles)
        if use_radians:
            this_angles = this_angles / 180 * np.pi
            assert np.all(this_angles[this_angles_nonnan] >= -np.pi) and np.all(
                this_angles[this_angles_nonnan] <= np.pi
            )
        else:
            assert np.all(this_angles[this_angles_nonnan] >= -180) and np.all(
                this_angles[this_angles_nonnan] <= 180
            )
        values.append(np.concatenate((this_dists, this_angles)))

    retval = np.array(values, dtype=np.float64)
    np.nan_to_num(retval, copy=False)  # Replace nan with 0 and info with large num
    assert retval.shape == (
        len(residues),
        len(distances) + len(angles),
    ), f"Got mismatched shapes {retval.shape} != {(len(residues), len(distances) + len(angles))}"
    return pd.DataFrame(retval, columns=distances + angles)


def sample_coords(
    fname: str,
    subset_residues: Optional[Collection[str]] = None,
    query_atoms: List[str] = ["N", "CA", "C", "O", "CB"],
) -> List[pd.DataFrame]:
    """
    Sample the atomic coordinates of Alanine atoms. Return a list of dataframes each containing these
    coordinates. 

    We use this to help figure out where to initialize atoms when creating a new chain
    """
    atomic_coords = []

    parser = PDB.PDBParser(QUIET=True)
    s = parser.get_structure("", fname)
    for chain in s.get_chains():
        residues = [
            r for r in chain.get_residues() if r.get_resname() not in ("HOH", "NA")
        ]

        for res in residues:
            if subset_residues is not None and res.get_resname() not in subset_residues:
                continue
            coords = {}
            for atom in res.get_atoms():
                coords[atom.get_name()] = atom.get_coord()
            all_atoms_present = True

            for atom in query_atoms:
                if atom not in coords:
                    logging.debug(f"{atom} not found in {res.get_resname()}")
                    all_atoms_present = False
                    break

            if all_atoms_present:
                atomic_coords.append(
                    pd.DataFrame([coords[k] for k in query_atoms], index=query_atoms)
                )
    return atomic_coords


def create_new_chain(
    out_fname: str, dists_and_angles: pd.DataFrame,
):
    """
    Creates a new chain. Note that input is radians and must be converted to normal degrees
    for PDB compatibility

    USeful references:
    https://stackoverflow.com/questions/47631064/create-a-polymer-chain-of-nonstandard-residues-from-a-single-residue-pdb
    """
    n = len(dists_and_angles)
    chain = PDB.Chain.Chain("A")
    # Avoid nonetype error
    chain.parent = PDB.Structure.Structure("pdb")

    rng = np.random.default_rng(seed=6489)

    # Assembly code
    # https://github.com/biopython/biopython/blob/4765a829258a776ac4c03b20b509e2096befba9d/Bio/PDB/internal_coords.py#L1393
    # appears to depend on chain's ordered_aa_ic_list whcih is a list of IC_Residues
    # https://biopython.org/docs/latest/api/Bio.PDB.internal_coords.html?highlight=ic_chain#Bio.PDB.internal_coords.IC_Residue
    # IC_residue extends https://biopython.org/docs/1.76/api/Bio.PDB.Residue.html
    # Set these IC_Residues
    for resnum, aa in enumerate(["ALA"] * n):  # Alanine is a single carbon sidechain
        # Constructor is ID, resname, segID
        # ID is 3-tuple of example (' ', 85, ' ')
        # resnum uses 1-indexing in real PDB files
        res = PDB.Residue.Residue((" ", resnum + 1, " "), aa, "A")
        # select a coordinate template for this atom
        # atoms in each resiude are N, CA, C, O, CB
        for atom in ["N", "CA", "C", "O", "CB"]:
            # https://biopython.org/docs/1.76/api/Bio.PDB.Atom.html
            # constructor expects
            # name, coord, bfactor, occupancy, altloc, fullname, serial_number
            # Generate a random coordinate
            # Occupancy is typically 1.0
            # Values under 10 create a model of the atom that is very sharp, indicating that the atom is not moving much and is in the same position in all of the molecules in the crystal
            # Values greater than 50 or so indicate that the atom is moving so much that it can barely been seen.
            coord = rng.random(3)
            atom_obj = PDB.Atom.Atom(
                atom, coord, 10.0, 1.0, " ", atom, resnum, element=atom[:1]
            )
            res.add(atom_obj)
        chain.add(res)

        # Convert residue to ic_residue
        ic_res = PDB.internal_coords.IC_Residue(res)
        ic_res.gly_Cbeta = True
        assert ic_res.is20AA

    # Write an intermediate to make sure we are modifying
    # io = PDB.PDBIO()
    # io.set_structure(chain)
    # io.save("intermediate.pdb")

    # Finished setting up the chain, now get the internal coordinates
    ic = PDB.internal_coords.IC_Chain(chain)
    # Initialize internal_coord data for loaded Residues.
    # Add IC_Residue as .internal_coord attribute for each Residue in parent Chain;
    # populate ordered_aa_ic_list with IC_Residue references for residues which can be built (amino acids and some hetatms)
    # set rprev and rnext on each sequential IC_Residue
    # populate initNCaC at start and after chain breaks

    # Determine which of the values are angles and which are distances
    angle_colnames = [c for c in dists_and_angles.columns if not ":" in c]
    dist_colnames = [c for c in dists_and_angles.columns if ":" in c]

    # Create placeholder values
    ic.atom_to_internal_coordinates()
    # ic.set_residues()
    for i, ric in enumerate(ic.ordered_aa_ic_list):
        assert isinstance(ric, PDB.internal_coords.IC_Residue)
        for angle in angle_colnames:
            ric.set_angle(angle, dists_and_angles.iloc[i][angle] / np.pi * 180)
        for dist in dist_colnames:
            d = dists_and_angles.iloc[i][dist]
            if np.isclose(d, 0):
                continue
            ric.set_length(dist, d)

    chain.internal_coord = ic

    chain.internal_to_atom_coordinates()

    # Write output
    io = PDB.PDBIO()
    io.set_structure(chain)
    io.save(out_fname)


def reverse_dihedral(v1, v2, v3, dihedral):
    """
    Find vector from c->d given a, b, c, & dihedral angle formed by a, b, c, d
    """
    # see https://github.com/pycogent/pycogent/blob/master/cogent/struct/dihedral.py
    def rotate(v, theta, axis):
        # https://stackoverflow.com/questions/6802577/rotation-of-3d-vector
        m = scipy.linalg.expm(
            np.cross(np.eye(3), axis / scipy.linalg.norm(axis) * theta)
        )
        return np.dot(v, m)

    v12 = v2 - v1
    v23 = v3 - v2
    # This is the first vector in the angle calculation that gives dihedral
    normal1 = np.cross(v12, v23)
    normal1 = normal1 / scipy.linalg.norm(normal1)

    rotated = rotate(normal1, dihedral, v12)

    # Invert cross product
    # https://math.stackexchange.com/questions/32600/whats-the-opposite-of-a-cross-product
    num = np.cross(rotated, v23)
    den = np.dot(v23, v23.T)
    final_offset = num / den  # Corresponds to V34
    final_offset /= scipy.linalg.norm(final_offset)

    return final_offset


def test_generation(
    reference_fname: str = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data/7PFL.pdb"
    )
):
    """
    Test the generation of a new chain
    """
    # sampled_coords = sample_coords(reference_fname)

    vals = canonical_distances_and_dihedrals(reference_fname)
    print(vals.iloc[:10])

    create_new_chain("test.pdb", vals)
    new_vals = canonical_distances_and_dihedrals("test.pdb")
    print(new_vals[:10])


def test_reverse_dihedral():
    """
    Test that we can reverse a dihedral
    """
    from sequence_models import pdb_utils

    a = np.array([[1.0, 0.0, 0.0]])
    b = np.array([[0.0, 0.0, 0.0]])
    c = np.array([[0.0, 1.0, 0.0]])
    d = np.array([[-1.0, 1.0, 0.0]])
    dh = pdb_utils.get_dihedrals(a, b, c, d)
    print(dh)

    reverse_dihedral(a, b, c, dh)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    # test_reverse_dihedral()
    test_generation()